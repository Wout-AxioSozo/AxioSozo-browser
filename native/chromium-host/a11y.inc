// Included by stream.inc right after input.inc (see docs/design/engine-accessibility.md
// §8 for the exact hook lines). Chromium accessibility for OSR targets:
//
//   * Zen enables a target's tree only while a macOS assistive client is active and
//     has asked for that canvas (`accessibility {enabled}`); CEF then runs renderer
//     accessibility in TreeOnly mode (kAXModeWebContentsOnly) for that browser only.
//   * CefAccessibilityHandler callbacks carry no browser, and the host shares one
//     cef_client_t, so each target gets its own thin client/render/accessibility
//     handler wrapper (AxClient) that copies the shared callbacks and adds only
//     get_accessibility_handler. The wrapper identifies the target.
//   * Tree and location payloads go through axio::ax::Mirror (a11y_core.hpp):
//     flat wire ids, password redaction, size caps, coalescing and credit-based
//     flow control (`ax_ack`), then out as `ax_tree_update` / `ax_location`.
//   * `ax_action` maps assistive actions onto the only C-API paths CEF 156 OSR has:
//     synthesized mouse/wheel/key input, frame select_all and IME commit. There is
//     no AXActionData entry point in the C API (limits in the design doc §5).
//
// All state is UI-thread owned, like the rest of stream.inc.
#include "include/capi/cef_accessibility_handler_capi.h"
#include "include/capi/cef_values_capi.h"
#include "a11y_core.hpp"
#include <cstddef>

static constexpr size_t AxValueBudget = 2000000;   // cef_value_t nodes converted per callback
static constexpr NSUInteger AxSetValueLimit = 4096;
static constexpr int AxActionsPerSecond = 20;
static constexpr int64_t AxFlushDelayMs = 16, AxRetryDelayMs = 50, AxFollowUpMs = 120;

struct AxClient;
// CEF structs first (standard layout) so each callback recovers its owner.
struct AxShell {
  cef_client_t client;
  cef_render_handler_t render;
  cef_accessibility_handler_t handler;
  AxClient* owner;
};
struct AxClient {
  AxShell shell{};
  std::atomic<int> refs{1};
  uint64_t serial=0;
  Tab* tab=nullptr;            // cleared by axTabClosed before the Tab is deleted
  bool enabled=false, flushScheduled=false;
  axio::ax::Mirror mirror;
  uint64_t rateWindow=0;int rateCount=0;
};
static std::map<uint64_t,AxClient*> axClients;  // live wrappers by serial
static uint64_t axLastSerial=0;
static double axNowMs(){return double(clock_gettime_nsec_np(CLOCK_UPTIME_RAW))/1e6;}

template<size_t Offset> static AxClient* axOwner(void* base){return reinterpret_cast<AxShell*>(reinterpret_cast<char*>(base)-Offset)->owner;}
static void axDestroy(AxClient* owner){
  // CEF may drop its last reference on any thread; all wrapper state is UI-thread owned.
  dispatch_async(dispatch_get_main_queue(),^{axClients.erase(owner->serial);delete owner;});
}
template<size_t Offset> static void axBase(cef_base_ref_counted_t& base,size_t size){
  base.size=size;
  base.add_ref=[](cef_base_ref_counted_t* b){axOwner<Offset>(b)->refs++;};
  base.release=[](cef_base_ref_counted_t* b)->int{AxClient* o=axOwner<Offset>(b);if(--o->refs==0){axDestroy(o);return 1;}return 0;};
  base.has_one_ref=[](cef_base_ref_counted_t* b)->int{return axOwner<Offset>(b)->refs==1;};
  base.has_at_least_one_ref=[](cef_base_ref_counted_t* b)->int{return axOwner<Offset>(b)->refs>=1;};
}
static AxClient* axFor(Tab* tab){
  if(!tab)return nullptr;
  for(auto& item:axClients)if(item.second->tab==tab)return item.second;
  return nullptr;
}

// ---- cef_value_t -> axio::ax::Value ----------------------------------------------------
static std::string axUtf8(const char16_t* text,size_t length){
  std::string out;out.reserve(length);
  for(size_t i=0;i<length;i++){
    uint32_t cp=text[i];
    if(cp>=0xD800&&cp<=0xDBFF&&i+1<length&&text[i+1]>=0xDC00&&text[i+1]<=0xDFFF){cp=0x10000+((cp-0xD800)<<10)+(text[i+1]-0xDC00);i++;}
    else if(cp>=0xD800&&cp<=0xDFFF)cp=0xFFFD;  // unpaired surrogate
    axio::ax::appendUtf8(out,cp);
  }
  return out;
}
static std::string axString(cef_string_userfree_t value){
  if(!value)return std::string();
  std::string out=axUtf8(value->str,std::min<size_t>(value->length,65536));
  p_cef_string_userfree_utf16_free(value);return out;
}
static bool axConvert(cef_value_t* v,axio::ax::Value& out,int depth,size_t& budget);
static bool axConvertDictionary(cef_dictionary_value_t* d,axio::ax::Value& out,int depth,size_t& budget){
  out.type=axio::ax::Value::Type::Dict;loadStringLists();
  cef_string_list_t keys=p_list_alloc();bool ok=d->get_keys(d,keys);
  size_t count=ok?p_list_size(keys):0;
  for(size_t i=0;ok&&i<count;i++){
    cef_string_t key{};if(!p_list_value(keys,i,&key))continue;
    cef_value_t* child=d->get_value(d,&key);
    std::string name=axUtf8(key.str,std::min<size_t>(key.length,256));if(key.dtor)key.dtor(key.str);
    if(!child)continue;
    out.fields.push_back({name,{}});ok=axConvert(child,out.fields.back().second,depth+1,budget);
  }
  p_list_free(keys);return ok;
}
// Bounded recursive copy; false when the budget ran out (the update is then dropped).
static bool axConvert(cef_value_t* v,axio::ax::Value& out,int depth,size_t& budget){
  HOLD(v);
  if(!v||depth>16||budget==0)return false;budget--;
  switch(v->get_type(v)){
    case VTYPE_BOOL:out.type=axio::ax::Value::Type::Bool;out.boolean=v->get_bool(v);return true;
    case VTYPE_INT:out.type=axio::ax::Value::Type::Number;out.number=v->get_int(v);return true;
    case VTYPE_DOUBLE:out.type=axio::ax::Value::Type::Number;out.number=v->get_double(v);return true;
    case VTYPE_STRING:out.type=axio::ax::Value::Type::String;out.text=axString(v->get_string(v));return true;
    case VTYPE_DICTIONARY:{cef_dictionary_value_t* d=v->get_dictionary(v);if(!d)return true;bool ok=axConvertDictionary(d,out,depth,budget);d->base.release(&d->base);return ok;}
    case VTYPE_LIST:{
      cef_list_value_t* l=v->get_list(v);if(!l)return true;out.type=axio::ax::Value::Type::List;bool ok=true;
      size_t count=l->get_size(l);out.items.reserve(std::min<size_t>(count,4096));
      for(size_t i=0;ok&&i<count;i++){cef_value_t* child=l->get_value(l,i);if(!child)continue;out.items.emplace_back();ok=axConvert(child,out.items.back(),depth+1,budget);}
      l->base.release(&l->base);return ok;
    }
    default:return true;  // null, binary, invalid: Null
  }
}

// ---- Flush / flow control ---------------------------------------------------------------
static void axScheduleFlush(AxClient* owner,int64_t delayMs);
static void axFlush(AxClient* owner){
  owner->flushScheduled=false;
  Tab* tab=owner->tab;
  if(!tab||!owner->enabled||tab->closing||streamClosing||!tab->target[@"native_target_id"])return;
  std::string target=json(tab->target);
  for(const auto& line:owner->mirror.flush(target,tab->width,tab->height,tab->scale,axNowMs()))
    if(!transport.event(line))return;  // a failed transport ends the host anyway
  // Throttled or waiting for credit: try again soon; an ack also reschedules.
  if(owner->mirror.pending()&&owner->mirror.outstanding()<axio::ax::MaxOutstanding)axScheduleFlush(owner,AxRetryDelayMs);
}
static void axScheduleFlush(AxClient* owner,int64_t delayMs){
  if(owner->flushScheduled)return;
  owner->flushScheduled=true;uint64_t serial=owner->serial;
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW,delayMs*int64_t(NSEC_PER_MSEC)),dispatch_get_main_queue(),^{
    auto found=axClients.find(serial);if(found!=axClients.end())axFlush(found->second);
  });
}

// ---- Per-target client wrapper (hook: create_browser takes axClientFor(tab)) -----------
static cef_client_t* axClientFor(Tab* tab){
  auto* owner=new AxClient();owner->serial=++axLastSerial;owner->tab=tab;owner->shell.owner=owner;
  AxShell& s=owner->shell;
  // Every shared callback ignores `self`; only the base and the two getters differ.
  s.client=client.api;s.render=render.api;
  axBase<offsetof(AxShell,client)>(s.client.base,sizeof(cef_client_t));
  axBase<offsetof(AxShell,render)>(s.render.base,sizeof(cef_render_handler_t));
  axBase<offsetof(AxShell,handler)>(s.handler.base,sizeof(cef_accessibility_handler_t));
  s.client.get_render_handler=[](cef_client_t* self){AxClient* o=axOwner<offsetof(AxShell,client)>(self);o->refs++;return &o->shell.render;};
  s.render.get_accessibility_handler=[](cef_render_handler_t* self){AxClient* o=axOwner<offsetof(AxShell,render)>(self);o->refs++;return &o->shell.handler;};
  s.handler.on_accessibility_tree_change=[](cef_accessibility_handler_t* self,cef_value_t* value){
    AxClient* o=axOwner<offsetof(AxShell,handler)>(self);
    if(!o->tab||!o->enabled||!value){if(value)value->base.release(&value->base);return;}
    @autoreleasepool{
      axio::ax::Value converted;size_t budget=AxValueBudget;
      // axConvert releases `value` (the reference CEF passed in).
      if(!axConvert(value,converted,0,budget)){tabEvent(o->tab,@"error",@{@"code":@"accessibility_update_too_large"});return;}
      o->mirror.treeChange(converted,axNowMs());axScheduleFlush(o,AxFlushDelayMs);
    }
  };
  s.handler.on_accessibility_location_change=[](cef_accessibility_handler_t* self,cef_value_t* value){
    AxClient* o=axOwner<offsetof(AxShell,handler)>(self);
    if(!o->tab||!o->enabled||!value){if(value)value->base.release(&value->base);return;}
    @autoreleasepool{
      axio::ax::Value converted;size_t budget=AxValueBudget;
      if(!axConvert(value,converted,0,budget))return;
      o->mirror.locationChange(converted);axScheduleFlush(o,AxFlushDelayMs);
    }
  };
  axClients[owner->serial]=owner;
  return &s.client;  // the initial reference is the one create_browser transfers to CEF
}
// Hook: lifespan on_before_close, before the Tab is deleted.
static void axTabClosed(Tab* tab){
  for(auto& item:axClients)if(item.second->tab==tab){item.second->tab=nullptr;item.second->enabled=false;item.second->mirror.disable();}
}
static NSArray* axActionNames(){return @[@"press",@"focus",@"scroll_to",@"set_value",@"show_menu",@"increment",@"decrement"];}
// Hook: ready capabilities `accessibility` and `ax_actions`.
static NSNumber* axCapability(){return @YES;}

// ---- Commands -----------------------------------------------------------------------------
// Hook: merged into streamCommand's exact-key table; `value` is optional on ax_action.
static NSDictionary* axCommandFields(){return @{@"accessibility":@[@"enabled"],@"ax_action":@[@"node_id",@"action"],@"ax_ack":@[@"seq"]};}
static NSArray* axOptionalFields(NSString* method){return [method isEqual:@"ax_action"]?@[@"value"]:@[];}

// `ax_ack` is like frame_ack: it names the exact target of the event it answers,
// which may predate a navigation, and only returns flow-control credit. Returns
// false for a protocol error. Hook: before streamCommand's current-target check.
static bool axAcknowledge(NSDictionary* supplied,NSDictionary* command){
  if(!integer(command[@"seq"])||![command[@"seq"] unsignedLongLongValue])return false;
  auto found=tabsByKey.find(keyOf(supplied[@"tab_id"]));
  AxClient* owner=found==tabsByKey.end()?nullptr:axFor(found->second);
  if(!owner||![supplied[@"native_target_id"] isEqual:found->second->target[@"native_target_id"]])return true;  // closed/replaced target: stale
  auto result=owner->mirror.ack([command[@"seq"] unsignedLongLongValue]);
  if(result==axio::ax::Mirror::Ack::Unknown)return false;
  if(result==axio::ax::Mirror::Ack::Released&&owner->mirror.pending())axScheduleFlush(owner,0);
  return true;
}

static void axMouse(Tab* tab,double x,double y,cef_mouse_button_type_t button){
  cef_mouse_event_t mouse{};mouse.x=int(std::lround(x));mouse.y=int(std::lround(y));mouse.modifiers=0;
  auto* host=tab->browser->get_host(tab->browser);
  host->send_mouse_move_event(host,&mouse,0);
  host->send_mouse_click_event(host,&mouse,button,0,1);host->send_mouse_click_event(host,&mouse,button,1,1);
  host->base.release(&host->base);
}
static bool axVisible(Tab* tab,const axio::ax::Rect& r,axio::ax::Rect& visible){
  double left=std::max(0.0,r.x),top=std::max(0.0,r.y),right=std::min(double(tab->width),r.x+r.w),bottom=std::min(double(tab->height),r.y+r.h);
  visible={left,top,right-left,bottom-top};return visible.w>=1&&visible.h>=1;
}
// Wheel the nearest scroller (or the page) until the node's top is about a third
// down the view. Chromium wheel deltas are pixels; positive deltaY scrolls up.
static void axScrollInto(Tab* tab,AxClient* owner,uint32_t node,const axio::ax::Rect& r){
  axio::ax::Rect area{0,0,double(tab->width),double(tab->height)},container;
  if(owner->mirror.scrollContainer(node,tab->width,tab->scale,container)&&axVisible(tab,container,container))area=container;
  double margin=std::min(area.h/3,48.0);
  double dy=0,dx=0;
  if(r.y<area.y)dy=area.y+margin-r.y;else if(r.y+std::min(r.h,area.h*0.8)>area.y+area.h)dy=-(r.y-area.y-margin);
  if(r.x<area.x)dx=area.x-r.x;else if(r.x+std::min(r.w,area.w)>area.x+area.w)dx=-(r.x+std::min(r.w,area.w)-area.x-area.w);
  cef_mouse_event_t mouse{};mouse.x=int(area.x+area.w/2);mouse.y=int(area.y+area.h/2);
  auto* host=tab->browser->get_host(tab->browser);
  for(int step=0;step<8&&(std::fabs(dx)>=1||std::fabs(dy)>=1);step++){
    int sx=int(std::clamp(dx,-2000.0,2000.0)),sy=int(std::clamp(dy,-2000.0,2000.0));
    host->send_mouse_wheel_event(host,&mouse,sx,sy);dx-=sx;dy-=sy;
  }
  host->base.release(&host->base);
}
static bool axEditable(const axio::ax::Node* n){return n&&(n->states&axio::ax::StateEditable);}
static void axKey(Tab* tab,int nativeCode,int windowsCode){
  auto* host=tab->browser->get_host(tab->browser);
  cef_key_event_t key{};key.size=sizeof(key);key.native_key_code=nativeCode;key.windows_key_code=windowsCode;
  key.character=key.unmodified_character=appKitCharacter(nativeCode);
  key.type=KEYEVENT_RAWKEYDOWN;host->send_key_event(host,&key);
  key.type=KEYEVENT_KEYUP;host->send_key_event(host,&key);
  host->base.release(&host->base);
}
// Runs `then` on the UI thread after `ms` if the target and its wrapper still exist.
static void axLater(AxClient* owner,int64_t ms,void(^then)(Tab*,AxClient*)){
  uint64_t serial=owner->serial;
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW,ms*int64_t(NSEC_PER_MSEC)),dispatch_get_main_queue(),^{
    auto found=axClients.find(serial);if(found==axClients.end())return;
    AxClient* o=found->second;Tab* tab=o->tab;
    if(!tab||!tab->browser||tab->closing||streamClosing)return;
    then(tab,o);
  });
}

// Returns false when `method` is not an accessibility command handled here.
static bool axCommand(Tab* tab,NSString* method,NSDictionary* command,NSString* requestId){
  if(![@[@"accessibility",@"ax_action"] containsObject:method])return false;
  AxClient* owner=axFor(tab);
  if([method isEqual:@"accessibility"]){
    if(!boolean(command[@"enabled"])){failProtocol();return true;}
    if(!owner){unsupported(requestId,@"accessibility_unavailable");return true;}
    event(@"accepted",@{@"request_id":requestId});
    bool enable=[command[@"enabled"] boolValue];
    if(enable!=owner->enabled){
      owner->enabled=enable;
      if(enable)owner->mirror.reset();else owner->mirror.disable();
      auto* host=tab->browser->get_host(tab->browser);host->set_accessibility_state(host,enable?STATE_ENABLED:STATE_DISABLED);host->base.release(&host->base);
      if(enable)axScheduleFlush(owner,AxFlushDelayMs);
    }
    complete(requestId,@"success");return true;
  }
  // ax_action {node_id, action, [value]}
  NSString* action=command[@"action"];id value=command[@"value"];
  if(!integer(command[@"node_id"],UINT32_MAX)||![command[@"node_id"] unsignedIntValue]||!textValue(action,16)||![axActionNames() containsObject:action]
      ||(value&&![action isEqual:@"set_value"])||([action isEqual:@"set_value"]&&!textValue(value,AxSetValueLimit))){failProtocol();return true;}
  if(!owner||!owner->enabled){unsupported(requestId,@"accessibility_disabled");return true;}
  uint64_t second=uint64_t(axNowMs()/1000);
  if(second!=owner->rateWindow){owner->rateWindow=second;owner->rateCount=0;}
  if(++owner->rateCount>AxActionsPerSecond){unsupported(requestId,@"rate_limited");return true;}
  uint32_t node=[command[@"node_id"] unsignedIntValue];
  const axio::ax::Node* n=owner->mirror.node(node);
  axio::ax::Rect r,visible;
  if(!n||n->hidden||!owner->mirror.rect(node,tab->width,tab->scale,r)){unsupported(requestId,@"stale_node");return true;}
  event(@"accepted",@{@"request_id":requestId});
  bool focused=owner->mirror.focus()==node;
  if([action isEqual:@"scroll_to"]){axScrollInto(tab,owner,node,r);complete(requestId,@"success");return true;}
  if([action isEqual:@"press"]||[action isEqual:@"show_menu"]){
    cef_mouse_button_type_t button=[action isEqual:@"press"]?MBT_LEFT:MBT_RIGHT;
    if(axVisible(tab,r,visible)){axMouse(tab,visible.x+visible.w/2,visible.y+visible.h/2,button);complete(requestId,@"success");return true;}
    // Off screen: scroll, then click where it landed (location changes arrive meanwhile).
    axScrollInto(tab,owner,node,r);
    axLater(owner,AxFollowUpMs,^(Tab* t,AxClient* o){
      axio::ax::Rect now,shown;
      if(o->mirror.node(node)&&o->mirror.rect(node,t->width,t->scale,now)&&axVisible(t,now,shown)){axMouse(t,shown.x+shown.w/2,shown.y+shown.h/2,button);complete(requestId,@"success");}
      else unsupported(requestId,@"offscreen");
    });
    return true;
  }
  if([action isEqual:@"increment"]||[action isEqual:@"decrement"]){
    // Sliders and spin buttons step with arrow keys once they have focus.
    if(!focused){unsupported(requestId,@"focus_required");return true;}
    bool up=[action isEqual:@"increment"];axKey(tab,up?0x7E:0x7D,up?0x26:0x28);
    complete(requestId,@"success");return true;
  }
  // focus / set_value need a focused editable; other elements cannot be focused
  // without activating them through the C API (no AXActionData/kFocus path).
  if(!axEditable(n)){unsupported(requestId,[action isEqual:@"focus"]?@"focus_unavailable":@"not_editable");return true;}
  if(!focused){
    if(!axVisible(tab,r,visible)){axScrollInto(tab,owner,node,r);unsupported(requestId,@"offscreen");return true;}
    // A click near the start of the field focuses it without activating anything.
    axMouse(tab,visible.x+std::min(3.0,visible.w/2),visible.y+visible.h/2,MBT_LEFT);
  }
  if([action isEqual:@"focus"]){complete(requestId,@"success");return true;}
  // set_value: replace the field's text as an IME commit would. Never logged.
  NSString* text=[value copy];
  axLater(owner,focused?0:AxFollowUpMs,^(Tab* t,AxClient* o){
    (void)o;
    auto* frame=t->browser->get_focused_frame(t->browser);
    if(!frame){unsupported(requestId,@"no_focused_frame");return;}
    frame->select_all(frame);frame->base.release(&frame->base);
    auto* host=t->browser->get_host(t->browser);auto utf=utf16(text);auto string=view(utf);
    cef_range_t invalid{UINT32_MAX,UINT32_MAX};host->ime_commit_text(host,&string,&invalid,0);host->base.release(&host->base);
    std::fill(utf.begin(),utf.end(),u'\0');
    complete(requestId,@"success");
  });
  return true;
}
