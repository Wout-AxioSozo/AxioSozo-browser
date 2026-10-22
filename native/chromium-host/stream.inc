// Included by host.mm after CEF/refcount helpers. All state below is UI-thread-owned
// except immutable fixtureOrigin and the bounded, synchronized transport itself.
// One host serves every Chromium tab of one Zen profile; each tab owns one target.
#include <deque>
#include <map>
static constexpr uint64_t MaxGeneration=9007199254740991ULL;
static constexpr size_t MaxTabs=32;
static uint64_t lastWebRequest=0;
static NSString* profilePath;
// Optional Mach IOSurface transport (contracts/engine-surface-v1.md). Absent
// surface_service keeps the AXCF BGRA pipe as the only frame path.
static bool surfaceMode=false, surfaceBeginFrames=false, surfaceFailed=false;
static int frameRate=60;
static NSString* surfaceFailure=nil;
struct Tab {
  NSString* key=nil;               // Zen tab_id; stable for this target's lifetime
  NSDictionary* target=nil;
  cef_browser_t* browser=nullptr;
  int width=900,height=650; double scale=2;
  bool paintAllowed=false, closing=false;
  NSString *createRequest=nil,*closeRequest=nil,*navigationRequest=nil;
  bool historyNavigation=false, commandNavigationInvalidated=false, sameDocumentNavigation=false;
  NSString *historyURL=nil,*committedDocumentURL=nil;
  // Main-frame RenderFrameHost whose OnLoadStart (commit) belongs to the current
  // navigation. OnLoadEnd from any other frame is a previous document finishing
  // late, e.g. the committed TLS interstitial of a load already failed by
  // OnLoadError; it never completes, authorizes or revokes the current load.
  NSString* documentFrame=nil;
  // Loads replaced by a newer command still report ERR_ABORTED; that report is
  // not about the current request. navigateURL is the current request's URL.
  int supersededLoads=0; NSString* navigateURL=nil;
  NSMutableSet* committedURLs=[NSMutableSet set];
  bool popupVisible=false; cef_rect_t popupRect{};
  int popupWidth=0,popupHeight=0,cachedWidth=0,cachedHeight=0;
  std::vector<uint8_t> popupPixels,cachedView;
  axio::surface::Target* surface=nullptr;  // surface mode only
  uint64_t lastBeginFrame=0;               // last applied BEGIN_FRAME sequence
  uint64_t lastCaptureCounter=0;           // last CEF capture counter sent to Zen
  bool refreshAfterRelease=false;          // a paint was dropped for lack of a free slot
  // Key verdicts (input.inc): key `down` requests awaiting the renderer's answer.
  struct KeyWait{uint64_t seq=0;NSString* request=nil;int nativeKeyCode=0;};
  std::deque<KeyWait> keyWaits;
  std::deque<uint64_t> probesInFlight;     // sequence of the keydown each probe follows
  uint64_t keySeq=0,probeSent=0,probeArrived=0;
  // Text input state (input.inc): 0 none, 1 text, 2 password; caret in logical points.
  int textMode=0,sentMode=-1;double caret[4]={0,0,0,0},sentCaret[4]={0,0,0,0};
  bool textInputScheduled=false;uint64_t textInputAt=0;
  // Trackpad pinch synthesized as a two-point touch sequence (input.inc).
  bool pinching=false;double pinchScale=1,pinchSpan=0,pinchX=0,pinchY=0;uint64_t pinchAt=0;
  // Page icon: the chosen icon URL, its current download and the last data URL sent.
  NSString *iconURL=nil,*iconData=nil;uint64_t iconRequest=0;
};
static std::map<std::string,Tab*> tabsByKey;
static std::map<int,Tab*> tabsByBrowser;
static Tab* creating=nullptr;  // CEF creation is asynchronous; one create at a time.
// CEF adds one reference to every ref-counted struct it passes into a C callback;
// the callback owns and must release it. Passing a struct into CEF transfers one.
struct Held {
  cef_base_ref_counted_t* base;
  ~Held(){if(base)base->release(base);}
};
#define HOLD(value) Held held_##value{(value)?&(value)->base:nullptr}
static std::string keyOf(NSString* value){return std::string(value.UTF8String);}
static Tab* tabFor(cef_browser_t* value,bool allowCreating=false){
  if(!value)return nullptr;
  auto found=tabsByBrowser.find(value->get_identifier(value));
  // Only geometry queries arrive before OnAfterCreated for the target in creation.
  return found!=tabsByBrowser.end()?found->second:(allowCreating?creating:nullptr);
}
static void clearPopup(Tab* tab){tab->popupVisible=false;std::vector<uint8_t>().swap(tab->popupPixels);std::vector<uint8_t>().swap(tab->cachedView);axio::surface::setPopupVisible(tab->surface,false);}
static std::string json(NSDictionary* value){
  NSData* data=[NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingSortedKeys error:nil];
  return data?std::string((const char*)data.bytes,data.length):std::string();
}
static NSDictionary* object(const std::string& line){
  NSData* data=[NSData dataWithBytes:line.data() length:line.size()];
  id value=[NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
  return [value isKindOfClass:NSDictionary.class]?value:nil;
}
static bool keys(NSDictionary* value,NSArray* expected){
  if(![value isKindOfClass:NSDictionary.class]||value.count!=expected.count)return false;
  for(id key in value)if(![expected containsObject:key])return false;
  return true;
}
static bool integer(id value,uint64_t maximum=MaxGeneration){
  if(![value isKindOfClass:NSNumber.class]||CFGetTypeID((__bridge CFTypeRef)value)==CFBooleanGetTypeID())return false;
  double number=[value doubleValue];return std::isfinite(number)&&number>=0&&number<=maximum&&std::floor(number)==number;
}
static bool signedInteger(id value,int minimum,int maximum){
  if(![value isKindOfClass:NSNumber.class]||CFGetTypeID((__bridge CFTypeRef)value)==CFBooleanGetTypeID())return false;
  double number=[value doubleValue];return std::isfinite(number)&&number>=minimum&&number<=maximum&&std::floor(number)==number;
}
static bool boolean(id value){return value&&CFGetTypeID((__bridge CFTypeRef)value)==CFBooleanGetTypeID();}
static bool textValue(id value,NSUInteger maximum){return [value isKindOfClass:NSString.class]&&[value length]<=maximum;}
static bool identifier(id value){
  if(!textValue(value,128)||![value length])return false;
  return [value rangeOfCharacterFromSet:[[NSCharacterSet characterSetWithCharactersInString:@"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:/.-"] invertedSet]].location==NSNotFound;
}
static bool tokenValue(id value){
  return textValue(value,64)&&[value length]==64&&[value rangeOfCharacterFromSet:[[NSCharacterSet characterSetWithCharactersInString:@"0123456789abcdefABCDEF"] invertedSet]].location==NSNotFound;
}
static bool equalToken(id value){
  if(!tokenValue(value))return false;
  unsigned difference=0;for(NSUInteger i=0;i<64;i++)difference|=[value characterAtIndex:i]^[streamToken characterAtIndex:i];return difference==0;
}
static NSString* origin(NSURL* url){
  if(![url.scheme isEqual:@"http"]||![url.host isEqual:@"127.0.0.1"]||!url.port||url.port.intValue<1||url.port.intValue>65535||url.user||url.password)return nil;
  return [NSString stringWithFormat:@"http://127.0.0.1:%d",url.port.intValue];
}
static bool webURL(NSString* value,bool resource=false){
  if(!textValue(value,resource?65536:4096)||![value length])return false;
  if([value isEqual:@"about:blank"])return true;
  if([value rangeOfCharacterFromSet:NSCharacterSet.controlCharacterSet].location!=NSNotFound)return false;
  NSURL* url=[NSURL URLWithString:value];
  // Data/blob resources remain under Chromium's normal origin and CSP checks.
  // They are never an initial page, address-bar navigation or external protocol.
  if(resource&&([url.scheme isEqual:@"data"]||[url.scheme isEqual:@"blob"]))return true;
  return ([@[@"http",@"https"] containsObject:url.scheme]&&url.host.length&&!url.user&&!url.password&&
    (!url.port||(url.port.intValue>0&&url.port.intValue<=65535)));
}
static bool webOrigin(NSString* value){
  if(!webURL(value)||[value isEqual:@"about:blank"])return false;
  NSURL* url=[NSURL URLWithString:value];return !url.path.length&&!url.query&&!url.fragment;
}
static bool allowedURL(NSString* value,bool resource=false){
  if(webBrowsing)return webURL(value,resource);
  if(!textValue(value,4096))return false;
  NSURL* url=[NSURL URLWithString:value];
  if(![[origin(url) description] isEqual:fixtureOrigin]||url.fragment)return false;
  return ([url.path isEqual:@"/engine.html"]&&(!url.query||[url.query isEqual:@"page=2"]))||(resource&&[url.path isEqual:@"/favicon.ico"]&&!url.query);
}
static bool fragmentNavigation(Tab* tab,NSString* value){
  if(!webBrowsing||!tab->committedDocumentURL||[value isEqual:tab->committedDocumentURL])return false;
  NSURLComponents* next=[NSURLComponents componentsWithString:value];
  NSURLComponents* previous=[NSURLComponents componentsWithString:tab->committedDocumentURL];
  next.fragment=nil;previous.fragment=nil;return [next.string isEqual:previous.string];
}
static bool validTarget(NSDictionary* value,bool pending){
  NSArray* wanted=pending?@[@"tab_id",@"engine_instance",@"identity",@"document_generation",@"navigation_generation",@"private_mode"]:@[@"tab_id",@"engine",@"engine_instance",@"native_target_id",@"identity",@"document_generation",@"navigation_generation",@"private_mode"];
  // A web host serves many origins; each target names its own. Fixture targets
  // stay bound to the one synthetic origin supplied at the authenticated hello.
  bool identity=webBrowsing?webOrigin(value[@"identity"]):[value[@"identity"] isEqual:fixtureOrigin];
  return keys(value,wanted)&&identifier(value[@"tab_id"])&&identifier(value[@"engine_instance"])&&[value[@"engine_instance"] isEqual:streamInstance]&&identity&&integer(value[@"document_generation"])&&integer(value[@"navigation_generation"])&&boolean(value[@"private_mode"])&&![value[@"private_mode"] boolValue]&&(pending||([value[@"engine"] isEqual:@"chromium"]&&identifier(value[@"native_target_id"])));
}
static void failProtocol(){transport.reject();streamClosing=true;}
static void tabEvent(Tab* tab,NSString* kind,NSDictionary* fields){
  NSMutableDictionary* item=[fields mutableCopy];item[@"event"]=kind;item[@"version"]=@1;
  if(tab->target[@"native_target_id"]&&!item[@"target"])item[@"target"]=tab->target;
  transport.event(json(item));
}
static void complete(NSString* request,NSString* status){event(@"completed",@{@"request_id":request,@"status":status});}
static void unsupported(NSString* request,NSString* reason){event(@"completed",@{@"request_id":request,@"status":@"unsupported",@"reason":reason});}
static void streamCommand(const std::string& line);
#include "a11y_forward.inc"
static void pumpCommands(){
  for(int count=0;count<32&&!streamClosing;++count){auto line=transport.pop();if(!line)break;streamCommand(*line);}
}
static std::atomic<bool> pumpScheduled{false};
// Reader thread -> main queue (the CEF UI thread). Coalesced; the 10 ms timer
// remains as a fallback. Commands still execute only on the UI thread.
static void scheduleCommands(){
  if(pumpScheduled.exchange(true))return;
  dispatch_async(dispatch_get_main_queue(),^{pumpScheduled=false;if(streamReady||streamClosing)pumpCommands();});
}
static bool streamHandshake(){
  if(!transport.start(scheduleCommands))return false;
  auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(10);
  while(std::chrono::steady_clock::now()<deadline&&!transport.failed()&&!transport.eof()){
    if(auto line=transport.pop()){
      NSDictionary* hello=object(*line);
      bool web=[hello[@"browsing_mode"] isEqual:@"web"];
      NSMutableArray* fields=[(web?@[@"version",@"method",@"token",@"engine_instance",@"fixture_origin",@"browsing_mode"]:@[@"version",@"method",@"token",@"engine_instance",@"fixture_origin"]) mutableCopy];
      // Optional fields are all-or-nothing per feature and still exact-key checked.
      bool surface=hello[@"surface_service"]!=nil;
      if(surface)[fields addObjectsFromArray:@[@"surface_service",@"surface_token"]];
      if(hello[@"surface_begin_frames"])[fields addObject:@"surface_begin_frames"];
      if(hello[@"frame_rate"])[fields addObject:@"frame_rate"];
      if(!keys(hello,fields)||!integer(hello[@"version"],1)||[hello[@"version"] intValue]!=1||![hello[@"method"] isEqual:@"hello"]||!tokenValue(hello[@"token"])||!identifier(hello[@"engine_instance"])||!textValue(hello[@"fixture_origin"],web?4096:128))return false;
      NSURL* url=[NSURL URLWithString:hello[@"fixture_origin"]];
      if((web?!webURL(hello[@"fixture_origin"]):![origin(url) isEqual:hello[@"fixture_origin"]])||url.path.length||url.query||url.fragment||[url.scheme isEqual:@"about"])return false;
      if(hello[@"frame_rate"]&&(!integer(hello[@"frame_rate"],120)||([hello[@"frame_rate"] intValue]!=60&&[hello[@"frame_rate"] intValue]!=120)))return false;
      if(hello[@"surface_begin_frames"]&&(!surface||!boolean(hello[@"surface_begin_frames"])))return false;
      if(surface){
        NSString* service=hello[@"surface_service"];
        // A bootstrap name_t holds 127 bytes; only a conservative ASCII subset.
        if(!textValue(service,127)||service.length<8||[service rangeOfCharacterFromSet:[[NSCharacterSet characterSetWithCharactersInString:@"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"] invertedSet]].location!=NSNotFound)return false;
        if(!tokenValue(hello[@"surface_token"])||[hello[@"surface_token"] isEqual:hello[@"token"]])return false;
        uint8_t secret[AXIO_SURFACE_TOKEN_BYTES];NSString* hex=hello[@"surface_token"];
        for(unsigned i=0;i<AXIO_SURFACE_TOKEN_BYTES;i++){unsigned value=0;sscanf([[hex substringWithRange:NSMakeRange(i*2,2)] UTF8String],"%2x",&value);secret[i]=uint8_t(value);}
        std::string error;bool connected=axio::surface::connect(service.UTF8String,secret,5000,error);
        memset(secret,0,sizeof(secret));
        if(!connected){fprintf(stderr,"surface transport: %s\n",error.c_str());return false;}
        surfaceMode=true;surfaceBeginFrames=[hello[@"surface_begin_frames"] boolValue];
      }
      if(hello[@"frame_rate"])frameRate=[hello[@"frame_rate"] intValue];
      streamToken=hello[@"token"];streamInstance=hello[@"engine_instance"];fixtureOrigin=hello[@"fixture_origin"];webBrowsing=web;requestIds=[NSMutableSet set];return true;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  return false;
}
static void streamReadyEvent(){
  // These values are queried from the loaded framework, not merely headers.
  NSString* runtimeCEF=[NSString stringWithFormat:@"%d.%d.%d",p_cef_version_info(0),p_cef_version_info(1),p_cef_version_info(2)];
  NSString* runtimeChromium=[NSString stringWithFormat:@"%d.%d.%d.%d",p_cef_version_info(4),p_cef_version_info(5),p_cef_version_info(6),p_cef_version_info(7)];
  event(@"ready",@{@"cef":@CEF_VERSION,@"chromium":runtimeChromium,@"runtime_cef":runtimeCEF,@"runtime_chromium":runtimeChromium,@"platform":@"macosarm64",@"sandbox_configured":@YES,@"engine_instance":streamInstance,@"render_path":surfaceMode?@"native-osr-iosurface":@"native-osr-bgra",@"capabilities":@{@"surface":@(surfaceMode),@"external_begin_frame":@(surfaceBeginFrames),@"frame_rate":@(frameRate),@"observe":@YES,@"navigate":@YES,@"back":@YES,@"forward":@YES,@"reload":@YES,@"stop":@YES,@"close":@YES,@"input":@YES,@"edit":@YES,@"visibility":@YES,@"cursor":@YES,@"multi_target":@YES,@"max_targets":@(MaxTabs),@"persistent_profile":@(webBrowsing),@"open_in_tab":@(webBrowsing),@"devtools":@NO,@"ime":@YES,@"key_verdict":@YES,@"wheel_phases":@YES,@"pinch":@YES,@"frame_rate_command":@YES,@"private_mode":@NO,@"fixture_only":@(!webBrowsing),@"permissions":@(webBrowsing),@"downloads":@(webBrowsing),@"popups":@NO,@"accessibility":axCapability(),@"ax_actions":axActionNames(),@"file_dialogs":@(webBrowsing),@"javascript_dialogs":@(webBrowsing),
    @"heartbeat_interval_ms":@(HeartbeatIntervalMs),@"context_menu":@(webBrowsing),@"http_auth":@(webBrowsing),@"find":@(webBrowsing),@"zoom":@(webBrowsing),@"popup_blocked":@(webBrowsing),@"media_capture":@NO}});
}
static bool surface(id w,id h,id scale){
  if(!integer(w,4096)||!integer(h,4096)||![w intValue]||![h intValue]||![scale isKindOfClass:NSNumber.class]||CFGetTypeID((__bridge CFTypeRef)scale)==CFBooleanGetTypeID())return false;
  double d=[scale doubleValue];return std::isfinite(d)&&d>=1&&d<=4;
}
// Surface mode moves no pixels through the pipe, so the 32 MiB AXCF payload cap
// does not apply. Each physical side stays <= 4096 px (a quarter of the 16384 px
// Metal 2D-texture limit on Apple silicon), so one BGRA IOSurface is at most
// 64 MiB and one target's ring (AXIO_SURFACE_MAX_IN_FLIGHT = 6 lazily allocated
// slots plus the popup cache) stays below ~512 MiB of GPU-shared memory at the
// 4096 px bound (about 100 MiB at a typical 2072x2048 tab). The presenter steps
// the scale down in quarters beyond 4096 px (e.g. a fullscreen 5K window).
static constexpr double SurfaceMaxBytes=4096.0*4096.0*4.0;
static bool fitsSurface(id w,id h,id scale){
  double pw=std::ceil([w doubleValue]*[scale doubleValue]),ph=std::ceil([h doubleValue]*[scale doubleValue]);
  return pw<=4096&&ph<=4096&&pw*ph*4<=(surfaceMode?SurfaceMaxBytes:double(axio::MaxFrame));
}
static void streamFrame(Tab* tab,const void* pixels,int w,int h){
  if(!tab->target[@"native_target_id"]||!tab->paintAllowed||tab->closing||streamClosing)return;
  if(w<=0||h<=0||w>4096||h>4096||uint64_t(w)*h*4>axio::MaxFrame){tabEvent(tab,@"error",@{@"code":@"unsupported_surface"});tab->paintAllowed=false;return;}
  // A queued old-size paint can arrive after WasResized; do not relabel it.
  if(w!=int(std::ceil(tab->width*tab->scale))||h!=int(std::ceil(tab->height*tab->scale)))return;
  if(frameId==MaxGeneration){failProtocol();return;}
  NSDictionary* metadata=@{@"version":@1,@"target":tab->target,@"frame_id":@(++frameId),@"width":@(w),@"height":@(h),@"stride":@(w*4),@"device_scale":@(tab->scale),@"format":@"BGRA8"};
  transport.frame(keyOf(tab->key),json(metadata),json(tab->target),frameId,pixels,size_t(w)*h*4);
}
static void compositePopup(Tab* tab){
  if(tab->cachedView.empty()||tab->popupPixels.empty()||!tab->popupVisible)return;
  std::vector<uint8_t> composite=tab->cachedView;
  int left=int(std::round(tab->popupRect.x*tab->scale)),top=int(std::round(tab->popupRect.y*tab->scale));
  int startX=std::max(0,-left),startY=std::max(0,-top);
  int endX=std::min(tab->popupWidth,tab->cachedWidth-left),endY=std::min(tab->popupHeight,tab->cachedHeight-top);
  for(int y=startY;y<endY;y++)if(endX>startX)
    memcpy(composite.data()+(size_t(y+top)*tab->cachedWidth+left+startX)*4,
      tab->popupPixels.data()+(size_t(y)*tab->popupWidth+startX)*4,size_t(endX-startX)*4);
  streamFrame(tab,composite.data(),tab->cachedWidth,tab->cachedHeight);
}
static void streamSurfacePaint(Tab* tab,cef_paint_element_type_t type,const void* pixels,int w,int h){
  if(w<=0||h<=0||w>4096||h>4096||uint64_t(w)*h*4>axio::MaxFrame)return;
  if(type==PET_VIEW){
    if(!tab->popupVisible){streamFrame(tab,pixels,w,h);return;}
    tab->cachedWidth=w;tab->cachedHeight=h;tab->cachedView.assign((const uint8_t*)pixels,(const uint8_t*)pixels+size_t(w)*h*4);
    if(tab->popupPixels.empty())streamFrame(tab,pixels,w,h);else compositePopup(tab);
  }else if(type==PET_POPUP&&tab->popupVisible&&uint64_t(w)*h*4<=8*1024*1024){
    tab->popupWidth=w;tab->popupHeight=h;tab->popupPixels.assign((const uint8_t*)pixels,(const uint8_t*)pixels+size_t(w)*h*4);compositePopup(tab);
  }
}
static void invalidateView(cef_browser_t* value);
static void closePromptsFor(Tab* tab,NSString* reason,bool pageOnly);
static bool uiCommand(Tab* tab,NSString* method,NSDictionary* command,NSString* requestId);
static bool inputCommand(Tab* tab,NSString* method,NSDictionary* command,NSString* requestId);
static void sendKeyProbe(Tab* tab);
static void resetInput(Tab* tab);
static void cancelPinch(Tab* tab);
static void finishKeyWaits(Tab* tab);
static void surfaceFail(const char* reason){
  if(surfaceFailed)return;
  surfaceFailed=true;surfaceFailure=[NSString stringWithUTF8String:reason];
  event(@"error",@{@"code":@"surface_failed",@"reason":surfaceFailure});streamClosing=true;
}
static uint64_t targetId(Tab* tab){return strtoull([tab->target[@"native_target_id"] UTF8String],nullptr,10);}
static Tab* tabForId(uint64_t id){
  if(!id||id>uint64_t(INT_MAX))return nullptr;
  auto found=tabsByBrowser.find(int(id));return found==tabsByBrowser.end()?nullptr:found->second;
}
// Runs inside CEF's OnAcceleratedPaint: the pooled IOSurface is copied on the
// GPU before returning (cef_render_handler.h:160-167 forbids retaining it).
static void streamAcceleratedPaint(Tab* tab,cef_paint_element_type_t type,size_t count,const cef_rect_t* rects,const cef_accelerated_paint_info_t* info){
  uint64_t entry=mach_absolute_time();
  if(!surfaceMode||!info||!tab->surface||!tab->target[@"native_target_id"]||!tab->paintAllowed||tab->closing||streamClosing)return;
  if(info->format!=CEF_COLOR_TYPE_BGRA_8888){tabEvent(tab,@"error",@{@"code":@"unsupported_surface_format"});tab->paintAllowed=false;return;}
  const auto& extra=info->extra;
  axio::surface::Rect visible{extra.visible_rect.x,extra.visible_rect.y,extra.visible_rect.width,extra.visible_rect.height};
  if(frameId==MaxGeneration){failProtocol();return;}
  axio::surface::Rect dirty[AXIO_SURFACE_MAX_DIRTY];size_t dirtyCount=0;
  // Dirty rects are relative to the previous capture; any gap means full frame.
  bool contiguous=extra.has_capture_counter&&tab->lastCaptureCounter&&extra.capture_counter==tab->lastCaptureCounter+1;
  if(type==PET_VIEW&&contiguous&&count<=AXIO_SURFACE_MAX_DIRTY)
    for(size_t i=0;i<count;i++)dirty[dirtyCount++]={rects[i].x,rects[i].y,rects[i].width,rects[i].height};
  axio::surface::FrameMeta meta;
  meta.target=targetId(tab);meta.documentGeneration=[tab->target[@"document_generation"] unsignedLongLongValue];
  meta.navigationGeneration=[tab->target[@"navigation_generation"] unsignedLongLongValue];
  meta.frameId=frameId+1;meta.paintTime=entry;meta.beginFrameSequence=surfaceBeginFrames?tab->lastBeginFrame:0;
  meta.logicalWidth=uint32_t(tab->width);meta.logicalHeight=uint32_t(tab->height);meta.scale=tab->scale;
  meta.dirty=dirtyCount?dirty:nullptr;meta.dirtyCount=dirtyCount;
  axio::surface::Result result;
  if(type==PET_VIEW){
    int w=int(std::ceil(tab->width*tab->scale)),h=int(std::ceil(tab->height*tab->scale));
    // A queued old-size capture can arrive after WasResized; do not relabel it.
    if(visible.width!=w||visible.height!=h){tab->lastCaptureCounter=0;return;}
    result=axio::surface::paintView(tab->surface,info->shared_texture_io_surface,visible,w,h,meta);
  }else if(type==PET_POPUP&&tab->popupVisible){
    result=axio::surface::paintPopup(tab->surface,info->shared_texture_io_surface,visible,
      int(std::round(tab->popupRect.x*tab->scale)),int(std::round(tab->popupRect.y*tab->scale)),meta);
  }else return;
  if(result==axio::surface::Result::Sent){frameId++;if(type==PET_VIEW)tab->lastCaptureCounter=extra.has_capture_counter?extra.capture_counter:0;}
  else if(result==axio::surface::Result::NoFreeSlot){tab->refreshAfterRelease=true;tab->lastCaptureCounter=0;}
  else if(result==axio::surface::Result::Invalid){tab->lastCaptureCounter=0;tabEvent(tab,@"error",@{@"code":@"unsupported_surface"});}
  else if(result==axio::surface::Result::Failed)streamClosing=true;
}
static void startSurface(){
  if(!surfaceMode)return;
  axio::surface::Callbacks callbacks;
  callbacks.beginFrame=[](uint64_t id,uint64_t sequence){
    if(!surfaceBeginFrames){surfaceFail("surface_unexpected_begin_frame");return;}
    Tab* tab=tabForId(id);if(!tab)return;  // Target closed while the tick was queued.
    if(sequence<=tab->lastBeginFrame){surfaceFail("surface_begin_frame_replay");return;}
    tab->lastBeginFrame=sequence;
    if(!tab->browser||tab->closing||streamClosing)return;
    auto* host=tab->browser->get_host(tab->browser);host->send_external_begin_frame(host);host->base.release(&host->base);
  };
  callbacks.release=[](uint64_t id,uint64_t frame)->bool{
    Tab* tab=tabForId(id);
    // A closed target's frames died with its ring; the id must still be issued.
    if(!tab)return frame<=frameId;
    if(!axio::surface::release(tab->surface,frame))return false;
    if(tab->refreshAfterRelease&&tab->browser&&!tab->closing){tab->refreshAfterRelease=false;invalidateView(tab->browser);}
    return true;
  };
  callbacks.failed=[](const char* reason){surfaceFail(reason);};
  axio::surface::start(std::move(callbacks));
}
// CEF returns NULL (not an empty userfree string) for an empty value, e.g. the
// link URL of a context menu opened off a link; freeing NULL faults (E1 2026-09-30:
// every right-click outside a link/image hung the host in this free).
static NSString* requestString(cef_string_userfree_t value){if(!value)return @"";NSString* result=ns(value);p_cef_string_userfree_utf16_free(value);return result;}
static void resetNavigation(Tab* tab){tab->historyNavigation=false;tab->historyURL=nil;tab->commandNavigationInvalidated=false;tab->sameDocumentNavigation=false;tab->navigateURL=nil;}
static void invalidateNavigation(Tab* tab){
  uint64_t document=[tab->target[@"document_generation"] unsignedLongLongValue],navigation=[tab->target[@"navigation_generation"] unsignedLongLongValue];
  if(document==MaxGeneration||navigation==MaxGeneration){failProtocol();return;}
  // Page prompts belong to the document that asked; they close with its old target.
  closePromptsFor(tab,@"navigation",true);
  NSMutableDictionary* next=[tab->target mutableCopy];next[@"document_generation"]=@(document+1);next[@"navigation_generation"]=@(navigation+1);tab->target=[next copy];
  tab->documentFrame=nil;tab->paintAllowed=false;clearPopup(tab);transport.invalidatePendingFrame(keyOf(tab->key));tabEvent(tab,@"navigation",@{});
}
static void invalidateView(cef_browser_t* value){auto* host=value->get_host(value);host->invalidate(host,PET_VIEW);host->base.release(&host->base);}
static void restoreCancelledDocument(Tab* tab,cef_browser_t* value){
  if(!webBrowsing||!tab->committedDocumentURL)return;
  auto* frame=value->get_main_frame(value);NSString* actual=requestString(frame->get_url(frame)),*actualFrame=requestString(frame->get_identifier(frame));frame->base.release(&frame->base);
  if(![actual isEqual:tab->committedDocumentURL]||!allowedURL(actual))return;
  tab->documentFrame=actualFrame;
  // The cancelled candidate never replaced this committed document. Its target
  // generation remains revoked, but the actual retained page can paint again.
  tab->paintAllowed=true;tabEvent(tab,@"url",@{@"url":actual});
  tabEvent(tab,@"load",@{@"http_status":@0,@"restored_from_history":@NO,@"same_document":@YES});
  invalidateView(value);
}
static void closeTab(Tab* tab){
  tab->closing=true;tab->paintAllowed=false;transport.invalidatePendingFrame(keyOf(tab->key));
  if(tab->browser){auto*h=tab->browser->get_host(tab->browser);h->close_browser(h,0);h->base.release(&h->base);}
}
// Called from the UI tick. Shutdown closes every target before the loop exits.
static void streamCloseAll(){
  std::vector<Tab*> open;for(auto& item:tabsByKey)if(!item.second->closing)open.push_back(item.second);
  for(Tab* tab:open)closeTab(tab); // CEF may finish a close synchronously and erase it
}
static bool streamIdle(){return tabsByKey.empty()&&!creating;}
static void streamCommand(const std::string& line){
  NSDictionary* command=object(line);NSString* method=command[@"method"],*requestId=command[@"request_id"];
  if(!integer(command[@"version"],1)||[command[@"version"] intValue]!=1||!equalToken(command[@"token"])||!identifier(requestId)||!textValue(method,32)){failProtocol();return;}
  if(webBrowsing){
    // A long-lived 30 fps tab can send >100,000 ACKs per hour. Monotonic IDs
    // reject replay with constant memory and no short session lifetime limit.
    if(![requestId hasPrefix:@"cef-"]){failProtocol();return;}
    NSString* digits=[requestId substringFromIndex:4];
    if(!digits.length||digits.length>16||[digits hasPrefix:@"0"]||[digits rangeOfCharacterFromSet:[[NSCharacterSet decimalDigitCharacterSet] invertedSet]].location!=NSNotFound){failProtocol();return;}
    uint64_t sequence=strtoull(digits.UTF8String,nullptr,10);
    if(sequence<=lastWebRequest||sequence>MaxGeneration){failProtocol();return;}lastWebRequest=sequence;
  }else{
    if([requestIds containsObject:requestId]||requestIds.count>=100000){failProtocol();return;}[requestIds addObject:requestId];
  }
  NSDictionary* extra=@{@"create":@[@"url",@"width",@"height",@"device_scale"],@"navigate":@[@"url"],@"back":@[],@"forward":@[],@"reload":@[],@"stop":@[],@"resize":@[@"width",@"height",@"device_scale"],@"focus":@[@"focused"],@"visibility":@[@"visible"],@"edit":@[@"action"],@"key":@[@"type",@"native_key_code",@"windows_key_code",@"modifiers",@"text"],@"mouse":@[@"type",@"x",@"y",@"modifiers",@"button",@"click_count",@"mouse_leave"],@"wheel":@[@"x",@"y",@"modifiers",@"delta_x",@"delta_y"],@"frame_ack":@[@"frame_id"],@"close":@[],@"shutdown":@[],@"devtools":@[],
    @"context_menu_command":@[@"prompt_id",@"command",@"index"],@"dialog_reply":@[@"prompt_id",@"accept",@"text"],
    @"permission_reply":@[@"prompt_id",@"decision"],@"file_dialog_reply":@[@"prompt_id",@"paths"],@"download_reply":@[@"prompt_id",@"path"],
    @"auth_reply":@[@"prompt_id",@"accept",@"username",@"password"],@"download_control":@[@"download_id",@"action"],
    @"find":@[@"text",@"forward",@"match_case",@"find_next"],@"stop_finding":@[@"clear_selection"],@"zoom":@[@"level"],
    @"pinch":@[@"x",@"y",@"modifiers",@"phase",@"magnification"],@"frame_rate":@[@"frame_rate"],
    @"ime_set_composition":@[@"text",@"selection_start",@"selection_end"],@"ime_commit_text":@[@"text"],
    @"ime_finish_composing":@[],@"ime_cancel_composition":@[]};
  NSArray* fields=extra[method]?:axCommandFields()[method];if(!fields){failProtocol();return;}
  NSMutableArray* expected=[@[@"version",@"method",@"request_id",@"token"] mutableCopy];
  if(![method isEqual:@"shutdown"])[expected addObject:@"target"];
  [expected addObjectsFromArray:fields];
  // Optional fields stay exact-key checked: each is admitted only by name.
  // Wheel phases are one all-or-nothing group (`wheel_phases` capability).
  if([method isEqual:@"wheel"]&&(command[@"phase"]||command[@"momentum_phase"]||command[@"precise"]))[expected addObjectsFromArray:@[@"phase",@"momentum_phase",@"precise"]];
  NSArray* optional=@{@"ime_set_composition":@[@"underlines",@"replacement_range"],@"ime_commit_text":@[@"replacement_range",@"relative_cursor_pos"],
    @"ime_finish_composing":@[@"keep_selection"]}[method];
  for(NSString* name in optional)if(command[name])[expected addObject:name];
  for(NSString* name in axOptionalFields(method))if(command[name])[expected addObject:name];
  if(!keys(command,expected)){failProtocol();return;}
  if([method isEqual:@"shutdown"]){event(@"accepted",@{@"request_id":requestId});shutdownRequest=requestId;streamClosing=true;return;}
  bool creating_=[method isEqual:@"create"];
  NSDictionary* supplied=command[@"target"];
  if(!validTarget(supplied,creating_)){failProtocol();return;}
  if([method isEqual:@"frame_ack"]){
    // Exact outstanding target is intentionally used instead of current target:
    // discarded stale frames also need to release their one transport credit.
    if(!integer(command[@"frame_id"])||!transport.acknowledge([command[@"frame_id"] unsignedLongLongValue],json(supplied))){failProtocol();return;}
    return;
  }
  if([method isEqual:@"ax_ack"]){if(!axAcknowledge(supplied,command))failProtocol();return;}
  auto found=tabsByKey.find(keyOf(supplied[@"tab_id"]));
  Tab* tab=found==tabsByKey.end()?nullptr:found->second;
  if(creating_){
    if(tab||creating||!streamReady||streamClosing){event(@"error",@{@"request_id":requestId,@"code":@"invalid_lifecycle"});return;}
    if(tabsByKey.size()>=MaxTabs){unsupported(requestId,@"target_limit");return;}
    if(!allowedURL(command[@"url"])||!surface(command[@"width"],command[@"height"],command[@"device_scale"])){failProtocol();return;}
    if(!fitsSurface(command[@"width"],command[@"height"],command[@"device_scale"])){unsupported(requestId,@"surface_limit");return;}
    tab=new Tab();tab->key=supplied[@"tab_id"];tab->target=[supplied copy];tab->createRequest=requestId;
    tab->width=[command[@"width"] intValue];tab->height=[command[@"height"] intValue];tab->scale=[command[@"device_scale"] doubleValue];
    if(surfaceMode&&!(tab->surface=axio::surface::createTarget())){delete tab;unsupported(requestId,@"surface_unavailable");return;}
    creating=tab;tabsByKey[keyOf(tab->key)]=tab;
    cef_window_info_t wi{};wi.size=sizeof(wi);wi.windowless_rendering_enabled=1;wi.runtime_style=CEF_RUNTIME_STYLE_ALLOY;
    // Shared textures: CEF 156 OSR delivers viz capture IOSurfaces on macOS
    // (libcef/browser/osr/video_consumer_osr.cc:184-191) despite the stale
    // "Windows only" header comment. External begin frames are opt-in.
    wi.shared_texture_enabled=surfaceMode;wi.external_begin_frame_enabled=surfaceBeginFrames;
    cef_browser_settings_t settings{};settings.size=sizeof(settings);settings.windowless_frame_rate=frameRate;
    auto value=utf16(command[@"url"]);auto url=view(value);
    event(@"accepted",@{@"request_id":requestId});
    isolatedContext->base.base.add_ref(&isolatedContext->base.base); // CEF takes this reference
    if(!p_cef_browser_host_create_browser(&wi,axClientFor(tab),&url,&settings,nullptr,isolatedContext)){
      axTabClosed(tab);complete(requestId,@"failed");creating=nullptr;tabsByKey.erase(keyOf(tab->key));axio::surface::destroyTarget(tab->surface);delete tab;
    }
    return;
  }
  if(!tab||![tab->target isEqualToDictionary:supplied]){event(@"error",@{@"request_id":requestId,@"code":@"stale_target"});return;}
  if(!tab->browser||tab->closing||streamClosing){event(@"error",@{@"request_id":requestId,@"code":@"closed_target"});return;}
  cef_browser_t* browser=tab->browser;
  // A pending key-verdict probe goes out before any later command of this tab;
  // only the keydown's own `char` may precede it (see input.inc).
  if(!([method isEqual:@"key"]&&[command[@"type"] isEqual:@"char"]))sendKeyProbe(tab);
  if([method isEqual:@"devtools"]){unsupported(requestId,@"devtools_not_integrated");return;}
  if(inputCommand(tab,method,command,requestId))return;
  if(axCommand(tab,method,command,requestId))return;
  if(uiCommand(tab,method,command,requestId))return;
  if([method isEqual:@"navigate"]&&!allowedURL(command[@"url"])){failProtocol();return;}
  if([method isEqual:@"resize"]){
    if(!surface(command[@"width"],command[@"height"],command[@"device_scale"])){failProtocol();return;}
    if(!fitsSurface(command[@"width"],command[@"height"],command[@"device_scale"])){unsupported(requestId,@"surface_limit");return;}
  }
  if([method isEqual:@"focus"]&&!boolean(command[@"focused"])){failProtocol();return;}
  if([method isEqual:@"visibility"]&&!boolean(command[@"visible"])){failProtocol();return;}
  if([method isEqual:@"edit"]&&![@[@"copy",@"cut",@"paste",@"select_all",@"undo",@"redo"] containsObject:command[@"action"]]){failProtocol();return;}
  if([method isEqual:@"mouse"]){
    if(!integer(command[@"x"],tab->width)||!integer(command[@"y"],tab->height)||!integer(command[@"modifiers"],131071)){failProtocol();return;}
    if(![@[@"move",@"down",@"up"] containsObject:command[@"type"]]||![@[@"left",@"middle",@"right"] containsObject:command[@"button"]]||!integer(command[@"click_count"],3)||![command[@"click_count"] intValue]||!boolean(command[@"mouse_leave"])){failProtocol();return;}
  }
  bool navigation=[@[@"navigate",@"back",@"forward",@"reload"] containsObject:method];
  if(([method isEqual:@"back"]&&!browser->can_go_back(browser))||([method isEqual:@"forward"]&&!browser->can_go_forward(browser))){unsupported(requestId,@"history_boundary");return;}
  if(navigation&&tab->navigationRequest){
    // A newer explicit navigation replaces a slow one, as in any browser. The
    // replaced request completes without success; nothing is replayed.
    unsupported(tab->navigationRequest,@"NAVIGATION_SUPERSEDED");tab->navigationRequest=nil;resetNavigation(tab);tab->supersededLoads++;
  }
  event(@"accepted",@{@"request_id":requestId});
  auto* host=browser->get_host(browser);
  if(navigation){
    tab->navigationRequest=requestId;tab->historyNavigation=[method isEqual:@"back"]||[method isEqual:@"forward"];tab->historyURL=nil;
    tab->sameDocumentNavigation=[method isEqual:@"navigate"]&&fragmentNavigation(tab,command[@"url"]);
    tab->navigateURL=[method isEqual:@"navigate"]?command[@"url"]:nil;
    // BFCache restores can skip OnBeforeBrowse entirely. Revoke the previous
    // document before dispatching every accepted browser-issued navigation.
    invalidateNavigation(tab);tab->commandNavigationInvalidated=true;
    if(transport.failed()){host->base.release(&host->base);return;}
  }
  if([method isEqual:@"navigate"]){auto* frame=browser->get_main_frame(browser);auto value=utf16(command[@"url"]);auto url=view(value);frame->load_url(frame,&url);frame->base.release(&frame->base);}
  else if([method isEqual:@"back"])browser->go_back(browser);
  else if([method isEqual:@"forward"])browser->go_forward(browser);
  else if([method isEqual:@"reload"])browser->reload(browser);
  else if([method isEqual:@"stop"])browser->stop_load(browser);
  else if([method isEqual:@"resize"]){tab->width=[command[@"width"] intValue];tab->height=[command[@"height"] intValue];tab->scale=[command[@"device_scale"] doubleValue];transport.invalidatePendingFrame(keyOf(tab->key));host->notify_screen_info_changed(host);host->was_resized(host);}
  else if([method isEqual:@"focus"])host->set_focus(host,[command[@"focused"] boolValue]);
  else if([method isEqual:@"visibility"]){bool visible=[command[@"visible"] boolValue];if(!visible)cancelPinch(tab);host->was_hidden(host,!visible);if(visible)host->invalidate(host,PET_VIEW);else transport.invalidatePendingFrame(keyOf(tab->key));}
  else if([method isEqual:@"edit"]){
    auto* frame=browser->get_focused_frame(browser);
    if(!frame){host->base.release(&host->base);unsupported(requestId,@"no_focused_frame");return;}
    NSString* action=command[@"action"];
    if([action isEqual:@"copy"])frame->copy(frame);else if([action isEqual:@"cut"])frame->cut(frame);
    else if([action isEqual:@"paste"])frame->paste(frame);else if([action isEqual:@"select_all"])frame->select_all(frame);
    else if([action isEqual:@"undo"])frame->undo(frame);else if([action isEqual:@"redo"])frame->redo(frame);
    frame->base.release(&frame->base);
  }
  else if([method isEqual:@"mouse"]){
    cef_mouse_event_t mouse{};mouse.x=[command[@"x"] intValue];mouse.y=[command[@"y"] intValue];mouse.modifiers=[command[@"modifiers"] unsignedIntValue];
    if([command[@"type"] isEqual:@"move"])host->send_mouse_move_event(host,&mouse,[command[@"mouse_leave"] boolValue]);
    else {cef_mouse_button_type_t button=[command[@"button"] isEqual:@"left"]?MBT_LEFT:([command[@"button"] isEqual:@"right"]?MBT_RIGHT:MBT_MIDDLE);host->send_mouse_click_event(host,&mouse,button,[command[@"type"] isEqual:@"up"],[command[@"click_count"] intValue]);}
  }else if([method isEqual:@"close"]){tab->closeRequest=requestId;host->base.release(&host->base);closeTab(tab);return;}
  host->base.release(&host->base);
  if(!navigation)complete(requestId,@"success");
}
static NSString* cursorName(cef_cursor_type_t type){
  switch(type){
    case CT_HAND:return @"pointer"; case CT_IBEAM:return @"text"; case CT_VERTICALTEXT:return @"vertical-text";
    case CT_WAIT:return @"wait"; case CT_PROGRESS:return @"progress"; case CT_HELP:return @"help";
    case CT_CROSS:return @"crosshair"; case CT_MOVE:return @"move"; case CT_NOTALLOWED:case CT_NODROP:case CT_DND_NONE:return @"not-allowed";
    case CT_GRAB:return @"grab"; case CT_GRABBING:return @"grabbing"; case CT_CONTEXTMENU:return @"context-menu";
    case CT_CELL:return @"cell"; case CT_ALIAS:case CT_DND_LINK:return @"alias"; case CT_COPY:case CT_DND_COPY:return @"copy";
    case CT_ZOOMIN:return @"zoom-in"; case CT_NONE:return @"none";
    case CT_EASTRESIZE:case CT_WESTRESIZE:case CT_EASTWESTRESIZE:case CT_COLUMNRESIZE:return @"ew-resize";
    case CT_NORTHRESIZE:case CT_SOUTHRESIZE:case CT_NORTHSOUTHRESIZE:case CT_ROWRESIZE:return @"ns-resize";
    case CT_NORTHEASTRESIZE:case CT_SOUTHWESTRESIZE:case CT_NORTHEASTSOUTHWESTRESIZE:return @"nesw-resize";
    case CT_NORTHWESTRESIZE:case CT_SOUTHEASTRESIZE:case CT_NORTHWESTSOUTHEASTRESIZE:return @"nwse-resize";
    default:return @"default";
  }
}
// ---- Browser UI delegation (contracts/cef-v1.md "Browser UI delegation") -----------
// Chromium never draws browser UI. Each CEF UI callback becomes a `prompt` event with
// a host-issued prompt_id; Zen renders it with Firefox UI and answers once. A reply
// must name that prompt_id, its kind and the exact current target; page prompts are
// also bound to the generations they were issued for. Replies are single use.
// Timeout, navigation, reset, close and shutdown answer with the safe default
// (cancel/deny/stay), then emit `prompt_closed`. Nothing here is reachable from
// page content: commands arrive only over the authenticated private pipe.
enum PromptKind{PromptContextMenu,PromptDialog,PromptBeforeUnload,PromptPermission,PromptFile,PromptDownload,PromptAuth};
struct Prompt{
  uint64_t id=0;PromptKind kind=PromptDialog;Tab* tab=nullptr;NSDictionary* target=nil;
  bool pageBound=true;                 // cancelled by navigation (all but downloads)
  cef_base_ref_counted_t* callback=nullptr;  // one reference owned by this prompt
  cef_frame_t* frame=nullptr;          // context menu: the frame it was opened on
  NSString *link=nil,*source=nil,*misspelled=nil;NSArray* suggestions=nil;int mediaType=0;
  int fileMode=0;uint32_t download=0;uint64_t nativePrompt=0;
};
static std::map<uint64_t,Prompt*> prompts;
static uint64_t lastPrompt=0;
static constexpr size_t MaxPrompts=64,MaxPromptsPerTab=8;
struct ActiveDownload{Tab* tab=nullptr;bool accepted=false;cef_download_item_callback_t* control=nullptr;};
static std::map<uint32_t,ActiveDownload> activeDownloads;
static int pendingImageCopies=0;
static Ref<cef_find_handler_t> finds;
static Ref<cef_download_image_callback_t> imageCopy;
static decltype(&cef_string_list_alloc) p_list_alloc=nullptr;
static decltype(&cef_string_list_size) p_list_size=nullptr;
static decltype(&cef_string_list_value) p_list_value=nullptr;
static decltype(&cef_string_list_append) p_list_append=nullptr;
static decltype(&cef_string_list_free) p_list_free=nullptr;
static void loadStringLists(){
  if(p_list_alloc)return;
  symbol(p_list_alloc,"cef_string_list_alloc");symbol(p_list_size,"cef_string_list_size");symbol(p_list_value,"cef_string_list_value");
  symbol(p_list_append,"cef_string_list_append");symbol(p_list_free,"cef_string_list_free");
}
static NSArray* listStrings(cef_string_list_t list,size_t maximum){
  NSMutableArray* result=[NSMutableArray array];if(!list)return result;loadStringLists();
  size_t count=std::min(p_list_size(list),maximum);
  for(size_t i=0;i<count;i++){cef_string_t value{};if(p_list_value(list,i,&value)){[result addObject:ns(&value)];if(value.dtor)value.dtor(value.str);}else [result addObject:@""];}
  return result;
}
static NSString* clip(NSString* value,NSUInteger maximum){
  if(!value)return @"";
  // Never split a surrogate pair; a clipped string is display text, never a URL.
  if(value.length<=maximum)return value;NSRange range=[value rangeOfComposedCharacterSequencesForRange:NSMakeRange(0,maximum)];
  return [[value substringWithRange:NSMakeRange(0,std::min(range.length,maximum))] stringByAppendingString:@"…"];
}
// URLs are sent whole or not at all: a truncated URL would be a different address.
static NSString* wholeURL(NSString* value,NSUInteger maximum){return value.length&&value.length<=maximum&&allowedURL(value,true)?value:@"";}
static NSString* promptName(uint64_t id){return [NSString stringWithFormat:@"prompt-%llu",(unsigned long long)id];}
static NSString* kindName(PromptKind kind){
  switch(kind){case PromptContextMenu:return @"context_menu";case PromptDialog:return @"dialog";case PromptBeforeUnload:return @"before_unload";
    case PromptPermission:return @"permission";case PromptFile:return @"file_dialog";case PromptDownload:return @"download";case PromptAuth:return @"auth";}
  return @"dialog";
}
static void stayOnPage(Tab* tab){
  // The user (or a safe default) kept the page: the navigation that asked is cancelled.
  if(tab->navigationRequest){unsupported(tab->navigationRequest,@"NAVIGATION_CANCELLED");tab->navigationRequest=nil;resetNavigation(tab);}
  if(tab->browser)restoreCancelledDocument(tab,tab->browser);
}
// Safe default for every kind: nothing is granted, selected, uploaded or saved.
static void denyPrompt(Prompt* p,bool userDismissed=false){
  if(!p->callback)return;
  switch(p->kind){
    case PromptContextMenu:{auto* c=(cef_run_context_menu_callback_t*)p->callback;c->cancel(c);break;}
    case PromptDialog:{auto* c=(cef_jsdialog_callback_t*)p->callback;c->cont(c,0,nullptr);break;}
    case PromptBeforeUnload:{
      auto* c=(cef_jsdialog_callback_t*)p->callback;bool leave=streamClosing||p->tab->closing;c->cont(c,leave?1:0,nullptr);
      if(!leave)stayOnPage(p->tab);break;
    }
    case PromptPermission:{auto* c=(cef_permission_prompt_callback_t*)p->callback;c->cont(c,userDismissed?CEF_PERMISSION_RESULT_DENY:CEF_PERMISSION_RESULT_DISMISS);break;}
    case PromptFile:{auto* c=(cef_file_dialog_callback_t*)p->callback;c->cancel(c);break;}
    case PromptDownload:break; // An unexecuted before-download callback cancels the download.
    case PromptAuth:{auto* c=(cef_auth_callback_t*)p->callback;c->cancel(c);break;}
  }
}
static void releasePrompt(Prompt* p){
  if(p->callback)p->callback->release(p->callback);
  if(p->frame)p->frame->base.release(&p->frame->base);
  if(p->kind==PromptDownload&&!activeDownloads[p->download].accepted)activeDownloads.erase(p->download);
  prompts.erase(p->id);delete p;
}
// Answering or denying calls back into CEF, which can re-enter synchronously: a
// JS dialog's cont() runs Chromium's dialog reset, i.e. on_reset_dialog_state.
// A prompt therefore leaves `prompts` before any CEF call, so a re-entrant path
// never finds, answers or frees it a second time (E1 2026-09-30: answering an
// alert double-freed its Prompt and killed or hung the host).
static Prompt* detachPrompt(uint64_t id){
  auto found=prompts.find(id);if(found==prompts.end())return nullptr;
  Prompt* p=found->second;prompts.erase(found);return p;
}
static void closePrompt(Prompt* p,NSString* reason,bool deny=true){
  prompts.erase(p->id);
  // Zen learns first (while the target still matches), then the page is released.
  tabEvent(p->tab,@"prompt_closed",@{@"prompt_id":promptName(p->id),@"reason":reason});
  if(deny)denyPrompt(p);
  releasePrompt(p);
}
static void closePromptsFor(Tab* tab,NSString* reason,bool pageOnly){
  std::vector<uint64_t> matched;
  for(auto& item:prompts)if(item.second->tab==tab&&(!pageOnly||item.second->pageBound))matched.push_back(item.first);
  // Re-look each one up: closing one may already have closed another.
  for(uint64_t id:matched)if(Prompt* p=detachPrompt(id))closePrompt(p,reason);
}
static Prompt* issuePrompt(Tab* tab,PromptKind kind,cef_base_ref_counted_t* callback,NSDictionary* details,int seconds,bool pageBound=true){
  if(!webBrowsing||!tab||!tab->browser||tab->closing||streamClosing||!tab->target[@"native_target_id"])return nullptr;
  size_t perTab=0;for(auto& item:prompts)if(item.second->tab==tab)perTab++;
  if(prompts.size()>=MaxPrompts||perTab>=MaxPromptsPerTab){tabEvent(tab,@"error",@{@"code":@"prompt_limit"});return nullptr;}
  if(lastPrompt>=MaxGeneration){failProtocol();return nullptr;}
  uint64_t id=lastPrompt+1;
  NSMutableDictionary* item=[@{@"event":@"prompt",@"version":@1,@"target":tab->target,@"prompt_id":promptName(id),@"kind":kindName(kind),
    @"details":details,@"timeout_ms":@(seconds*1000)} mutableCopy];
  std::string encoded=json(item);
  // A prompt that cannot be described within one bounded event is denied, not truncated.
  if(encoded.empty()||encoded.size()>axio::MaxMeta-256){tabEvent(tab,@"error",@{@"code":@"prompt_too_large"});return nullptr;}
  auto* p=new Prompt();p->id=lastPrompt=id;p->kind=kind;p->tab=tab;p->target=tab->target;p->pageBound=pageBound;
  p->callback=callback;callback->add_ref(callback);prompts[id]=p;
  transport.event(encoded);
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW,int64_t(seconds)*int64_t(NSEC_PER_SEC)),dispatch_get_main_queue(),^{
    auto found=prompts.find(id);if(found!=prompts.end())closePrompt(found->second,@"timeout");
  });
  return p;
}
static void cancelDownloadsFor(Tab* tab){
  for(auto it=activeDownloads.begin();it!=activeDownloads.end();){
    if(it->second.tab!=tab){++it;continue;}
    if(it->second.control){it->second.control->cancel(it->second.control);it->second.control->base.release(&it->second.control->base);}
    it=activeDownloads.erase(it);
  }
}
static NSString* mediaName(int type){
  switch(type){case CM_MEDIATYPE_IMAGE:return @"image";case CM_MEDIATYPE_VIDEO:return @"video";case CM_MEDIATYPE_AUDIO:return @"audio";
    case CM_MEDIATYPE_CANVAS:return @"canvas";case CM_MEDIATYPE_FILE:return @"file";case CM_MEDIATYPE_PLUGIN:return @"plugin";default:return @"none";}
}
// Permissions Zen may ask about. Capture (camera/mic/screen) is refused natively:
// this ad hoc signed OSR host has no camera/microphone usage descriptions, so macOS
// privacy (TCC) cannot grant it; unknown kinds are denied rather than guessed.
static NSArray* permissionNames(uint32_t requested,bool* capture){
  static const std::pair<uint32_t,NSString*> known[]={
    {CEF_PERMISSION_TYPE_GEOLOCATION,@"geolocation"},{CEF_PERMISSION_TYPE_NOTIFICATIONS,@"notifications"},
    {CEF_PERMISSION_TYPE_CLIPBOARD,@"clipboard"},{CEF_PERMISSION_TYPE_STORAGE_ACCESS,@"storage_access"},
    {CEF_PERMISSION_TYPE_TOP_LEVEL_STORAGE_ACCESS,@"top_level_storage_access"},{CEF_PERMISSION_TYPE_DISK_QUOTA,@"persistent_storage"},
    {CEF_PERMISSION_TYPE_LOCAL_FONTS,@"local_fonts"},{CEF_PERMISSION_TYPE_IDLE_DETECTION,@"idle_detection"},
    {CEF_PERMISSION_TYPE_MIDI_SYSEX,@"midi_sysex"},{CEF_PERMISSION_TYPE_MULTIPLE_DOWNLOADS,@"multiple_downloads"},
    {CEF_PERMISSION_TYPE_KEYBOARD_LOCK,@"keyboard_lock"},{CEF_PERMISSION_TYPE_POINTER_LOCK,@"pointer_lock"},
    {CEF_PERMISSION_TYPE_WINDOW_MANAGEMENT,@"window_management"},{CEF_PERMISSION_TYPE_FILE_SYSTEM_ACCESS,@"file_system_access"},
    {CEF_PERMISSION_TYPE_SENSORS,@"sensors"},{1u<<25,@"local_network"}, // LOCAL_NETWORK_ACCESS (renamed across API versions)
    
    {CEF_PERMISSION_TYPE_LOCAL_NETWORK,@"local_network"},{CEF_PERMISSION_TYPE_LOOPBACK_NETWORK,@"local_network"}};
  uint32_t media=CEF_PERMISSION_TYPE_CAMERA_STREAM|CEF_PERMISSION_TYPE_MIC_STREAM|CEF_PERMISSION_TYPE_CAMERA_PAN_TILT_ZOOM|CEF_PERMISSION_TYPE_CAPTURED_SURFACE_CONTROL;
  *capture=(requested&media)!=0;if(*capture||!requested)return nil;
  NSMutableArray* names=[NSMutableArray array];uint32_t covered=0;
  for(auto& item:known)if(requested&item.first){covered|=item.first;if(![names containsObject:item.second])[names addObject:item.second];}
  return covered==requested?names:nil;
}
static bool absolutePath(id value){
  if(!textValue(value,4096)||![value length]||![value hasPrefix:@"/"]||[value rangeOfString:@"\0"].location!=NSNotFound)return false;
  for(NSString* part in [value pathComponents])if([part isEqual:@".."])return false;
  return true;
}
// Reply commands. Returns false when `method` is not a delegation command.
static bool uiCommand(Tab* tab,NSString* method,NSDictionary* command,NSString* requestId){
  static NSDictionary* promptKinds=@{@"context_menu_command":@[@(PromptContextMenu)],@"dialog_reply":@[@(PromptDialog),@(PromptBeforeUnload)],
    @"permission_reply":@[@(PromptPermission)],@"file_dialog_reply":@[@(PromptFile)],@"download_reply":@[@(PromptDownload)],@"auth_reply":@[@(PromptAuth)]};
  NSArray* kinds=promptKinds[method];
  bool other=[@[@"download_control",@"find",@"stop_finding",@"zoom"] containsObject:method];
  if(!kinds&&!other)return false;
  if(!webBrowsing){unsupported(requestId,@"fixture_only");return true;}
  cef_browser_t* browser=tab->browser;
  if(other){
    auto* host=browser->get_host(browser);bool ok=true;
    if([method isEqual:@"find"]){
      NSString* text=command[@"text"];
      if(!textValue(text,1024)||!text.length||!boolean(command[@"forward"])||!boolean(command[@"match_case"])||!boolean(command[@"find_next"])){host->base.release(&host->base);failProtocol();return true;}
      event(@"accepted",@{@"request_id":requestId});auto value=utf16(text);auto view_=view(value);
      host->find(host,&view_,[command[@"forward"] boolValue],[command[@"match_case"] boolValue],[command[@"find_next"] boolValue]);
    }else if([method isEqual:@"stop_finding"]){
      if(!boolean(command[@"clear_selection"])){host->base.release(&host->base);failProtocol();return true;}
      event(@"accepted",@{@"request_id":requestId});host->stop_finding(host,[command[@"clear_selection"] boolValue]);
    }else if([method isEqual:@"zoom"]){
      id level=command[@"level"];
      if(![level isKindOfClass:NSNumber.class]||CFGetTypeID((__bridge CFTypeRef)level)==CFBooleanGetTypeID()||!std::isfinite([level doubleValue])||fabs([level doubleValue])>10){host->base.release(&host->base);failProtocol();return true;}
      event(@"accepted",@{@"request_id":requestId});host->set_zoom_level(host,[level doubleValue]);
    }else{
      NSString* action=command[@"action"];
      if(!integer(command[@"download_id"],UINT32_MAX)||![@[@"cancel",@"pause",@"resume"] containsObject:action]){host->base.release(&host->base);failProtocol();return true;}
      auto found=activeDownloads.find([command[@"download_id"] unsignedIntValue]);
      ok=found!=activeDownloads.end()&&found->second.tab==tab&&found->second.accepted&&found->second.control;
      if(ok){
        event(@"accepted",@{@"request_id":requestId});auto* control=found->second.control;
        if([action isEqual:@"cancel"])control->cancel(control);else if([action isEqual:@"pause"])control->pause(control);else control->resume(control);
      }
    }
    host->base.release(&host->base);
    if(ok)complete(requestId,@"success");else event(@"error",@{@"request_id":requestId,@"code":@"stale_download"});
    return true;
  }
  NSString* name=command[@"prompt_id"];
  if(!identifier(name)||![name hasPrefix:@"prompt-"]){failProtocol();return true;}
  uint64_t number=strtoull([[name substringFromIndex:7] UTF8String],nullptr,10);
  auto found=prompts.find(number);Prompt* p=found==prompts.end()?nullptr:found->second;
  // Unknown, answered, expired, foreign-tab, wrong-kind or stale-generation replies
  // do nothing. Timeouts race legitimately with the user, so this is not fatal.
  if(!p||![promptName(number) isEqual:name]||p->tab!=tab||![kinds containsObject:@(p->kind)]||(p->pageBound&&![p->target isEqualToDictionary:tab->target])){
    event(@"error",@{@"request_id":requestId,@"code":@"stale_prompt"});return true;
  }
  prompts.erase(found);  // before any CEF call (see detachPrompt)
  auto* host=browser->get_host(browser);
  bool valid=true;
  if(p->kind==PromptContextMenu){
    NSString* action=command[@"command"];
    NSArray* actions=@[@"dismiss",@"copy",@"cut",@"paste",@"select_all",@"undo",@"redo",@"copy_image",@"save_image",@"save_link",@"spelling",@"add_to_dictionary"];
    valid=textValue(action,32)&&[actions containsObject:action]&&integer(command[@"index"],16);
    if(valid){
      event(@"accepted",@{@"request_id":requestId});
      cef_frame_t* frame=p->frame;int index=[command[@"index"] intValue];
      if(frame&&[action isEqual:@"copy"])frame->copy(frame);else if(frame&&[action isEqual:@"cut"])frame->cut(frame);
      else if(frame&&[action isEqual:@"paste"])frame->paste(frame);else if(frame&&[action isEqual:@"select_all"])frame->select_all(frame);
      else if(frame&&[action isEqual:@"undo"])frame->undo(frame);else if(frame&&[action isEqual:@"redo"])frame->redo(frame);
      else if(([action isEqual:@"save_image"]||[action isEqual:@"copy_image"])&&p->source.length){
        auto value=utf16(p->source);auto url=view(value);
        // Both use Chromium's own network context, cookies and cache for this page.
        if([action isEqual:@"save_image"])host->start_download(host,&url);
        else if(p->mediaType==CM_MEDIATYPE_IMAGE&&pendingImageCopies<4){pendingImageCopies++;host->download_image(host,&url,0,0,0,imageCopy.retain());}
      }else if([action isEqual:@"save_link"]&&p->link.length){auto value=utf16(p->link);auto url=view(value);host->start_download(host,&url);}
      else if([action isEqual:@"spelling"]&&index<(int)p->suggestions.count){auto value=utf16(p->suggestions[index]);auto word=view(value);host->replace_misspelling(host,&word);}
      else if([action isEqual:@"add_to_dictionary"]&&p->misspelled.length){auto value=utf16(p->misspelled);auto word=view(value);host->add_word_to_dictionary(host,&word);}
      denyPrompt(p); // closes the native menu model; the chosen action already ran
    }
  }else if(p->kind==PromptDialog||p->kind==PromptBeforeUnload){
    valid=boolean(command[@"accept"])&&textValue(command[@"text"],4096);
    if(valid){
      event(@"accepted",@{@"request_id":requestId});bool accept=[command[@"accept"] boolValue];
      auto* c=(cef_jsdialog_callback_t*)p->callback;auto value=utf16(command[@"text"]);auto text=view(value);
      c->cont(c,accept?1:0,p->kind==PromptDialog?&text:nullptr);
      if(p->kind==PromptBeforeUnload&&!accept)stayOnPage(tab);
    }
  }else if(p->kind==PromptPermission){
    NSString* decision=command[@"decision"];
    valid=textValue(decision,16)&&[@[@"allow",@"deny",@"dismiss"] containsObject:decision];
    if(valid){
      event(@"accepted",@{@"request_id":requestId});auto* c=(cef_permission_prompt_callback_t*)p->callback;
      c->cont(c,[decision isEqual:@"allow"]?CEF_PERMISSION_RESULT_ACCEPT:([decision isEqual:@"deny"]?CEF_PERMISSION_RESULT_DENY:CEF_PERMISSION_RESULT_DISMISS));
    }
  }else if(p->kind==PromptFile){
    NSArray* paths=command[@"paths"];NSFileManager* files=NSFileManager.defaultManager;
    valid=[paths isKindOfClass:NSArray.class]&&paths.count<=64&&(p->fileMode==FILE_DIALOG_OPEN_MULTIPLE||paths.count<=1);
    NSArray* candidates=valid?paths:@[];
    for(id path in candidates){
      BOOL directory=NO;bool exists=absolutePath(path)&&[files fileExistsAtPath:path isDirectory:&directory];
      if(p->fileMode==FILE_DIALOG_SAVE){
        BOOL parentDirectory=NO;
        valid=valid&&absolutePath(path)&&[files fileExistsAtPath:[path stringByDeletingLastPathComponent] isDirectory:&parentDirectory]&&parentDirectory&&!(exists&&directory);
      }else valid=valid&&exists&&(p->fileMode==FILE_DIALOG_OPEN_FOLDER?directory:!directory);
    }
    if(valid){
      event(@"accepted",@{@"request_id":requestId});auto* c=(cef_file_dialog_callback_t*)p->callback;
      if(!paths.count)c->cancel(c);
      else{
        loadStringLists();cef_string_list_t list=p_list_alloc();
        for(NSString* path in paths){auto value=utf16(path);auto item=view(value);p_list_append(list,&item);}
        c->cont(c,list);p_list_free(list);
      }
    }
  }else if(p->kind==PromptDownload){
    NSString* path=command[@"path"];
    BOOL directory=NO,parentDirectory=NO;NSFileManager* files=NSFileManager.defaultManager;
    valid=textValue(path,4096)&&(!path.length||(absolutePath(path)&&[files fileExistsAtPath:[path stringByDeletingLastPathComponent] isDirectory:&parentDirectory]&&parentDirectory
      &&!([files fileExistsAtPath:path isDirectory:&directory]&&directory)));
    if(valid){
      event(@"accepted",@{@"request_id":requestId});
      if(path.length){auto* c=(cef_before_download_callback_t*)p->callback;auto value=utf16(path);auto target=view(value);c->cont(c,&target,0);activeDownloads[p->download].accepted=true;}
    }
  }else if(p->kind==PromptAuth){
    valid=boolean(command[@"accept"])&&textValue(command[@"username"],1024)&&textValue(command[@"password"],1024);
    if(valid){
      event(@"accepted",@{@"request_id":requestId});auto* c=(cef_auth_callback_t*)p->callback;
      if([command[@"accept"] boolValue]){auto user=utf16(command[@"username"]),pass=utf16(command[@"password"]);auto u=view(user),w=view(pass);c->cont(c,&u,&w);
        std::fill(pass.begin(),pass.end(),u'\0');}
      else c->cancel(c);
    }
  }
  host->base.release(&host->base);
  if(!valid){denyPrompt(p);releasePrompt(p);failProtocol();return true;}
  // Answered exactly once: the callback reference is dropped without a second call.
  tabEvent(tab,@"prompt_closed",@{@"prompt_id":name,@"reason":@"answered"});
  releasePrompt(p);complete(requestId,@"success");
  return true;
}
// ---- Page icon --------------------------------------------------------------
// Chromium downloads the page's declared icon (or its default /favicon.ico) with
// its own network context; cookies are neither sent nor stored for favicons.
// Zen receives only a small PNG data URL and never fetches the icon itself.
constexpr uint32_t IconPixels=32;      // 16 pt tab icon at 2x
constexpr size_t MaxIconBytes=5400;    // base64 + target stays within transport MaxMeta
static uint64_t iconRequests=0;
static void sendIcon(Tab* tab,NSString* data){
  tab->iconData=data;
  NSMutableDictionary* item=[@{@"event":@"favicon",@"version":@1,@"icon":data} mutableCopy];
  if(!tab->target[@"native_target_id"])return;
  item[@"target"]=tab->target;std::string encoded=json(item);
  if(encoded.size()>axio::MaxMeta-64){tab->iconData=@"";item[@"icon"]=@"";encoded=json(item);}
  transport.event(encoded);
}
static NSString* iconDataURL(cef_image_t* image){
  if(!image||image->is_empty(image))return @"";
  int w=0,h=0;cef_binary_value_t* png=image->get_as_png(image,1.0f,1,&w,&h);if(!png)return @"";
  size_t size=png->get_size(png);NSString* result=@"";
  if(size&&size<=MaxIconBytes&&w>0&&h>0){
    NSMutableData* data=[NSMutableData dataWithLength:size];png->get_data(png,data.mutableBytes,size,0);
    result=[@"data:image/png;base64," stringByAppendingString:[data base64EncodedStringWithOptions:0]];
  }
  png->base.release(&png->base);return result;
}
// One icon download. CEF owns the only reference; the last release frees it.
struct IconDownload {
  cef_download_image_callback_t api{};std::atomic<int> refs{1};NSString* key=nil;uint64_t request=0;
  IconDownload(){
    api.base.size=sizeof(api);
    api.base.add_ref=[](cef_base_ref_counted_t* b){reinterpret_cast<IconDownload*>(b)->refs++;};
    api.base.release=[](cef_base_ref_counted_t* b){auto* p=reinterpret_cast<IconDownload*>(b);if(--p->refs==0){delete p;return 1;}return 0;};
    api.base.has_one_ref=[](cef_base_ref_counted_t* b){return int(reinterpret_cast<IconDownload*>(b)->refs==1);};
    api.base.has_at_least_one_ref=[](cef_base_ref_counted_t* b){return int(reinterpret_cast<IconDownload*>(b)->refs>0);};
    api.on_download_image_finished=[](cef_download_image_callback_t* self,const cef_string_t*,int,cef_image_t* image){
      HOLD(image);auto* d=reinterpret_cast<IconDownload*>(self);
      auto found=tabsByKey.find(keyOf(d->key));Tab* tab=found!=tabsByKey.end()?found->second:nullptr;
      // A newer icon, document or target replaced this request.
      if(!tab||tab->closing||tab->iconRequest!=d->request)return;
      sendIcon(tab,iconDataURL(image));
    };
  }
};
static void requestIcon(Tab* tab,cef_browser_t* value,NSString* url){
  if(!url){tab->iconURL=nil;tab->iconRequest=++iconRequests;sendIcon(tab,@"");return;}
  // The same icon for the next document: resend what Zen already showed.
  if([url isEqual:tab->iconURL]&&tab->iconData){sendIcon(tab,tab->iconData);return;}
  tab->iconURL=url;tab->iconData=nil;tab->iconRequest=++iconRequests;
  auto* download=new IconDownload();download->key=tab->key;download->request=tab->iconRequest;
  auto text=utf16(url);auto address=view(text);
  auto* host=value->get_host(value);host->download_image(host,&address,1,IconPixels,0,&download->api);host->base.release(&host->base);
}
static void configureDelegation(){
  client.api.get_find_handler=[](cef_client_t*){return finds.retain();};
  finds.api.on_find_result=[](cef_find_handler_t*,cef_browser_t* value,int identifier,int count,const cef_rect_t*,int active,int final_){
    HOLD(value);Tab* tab=tabFor(value);if(!tab||!webBrowsing)return;
    tabEvent(tab,@"find_result",@{@"identifier":@(std::max(0,identifier)),@"count":@(std::max(0,count)),@"active":@(std::max(0,active)),@"final":@(bool(final_))});
  };
  imageCopy.api.on_download_image_finished=[](cef_download_image_callback_t*,const cef_string_t*,int,cef_image_t* image){
    HOLD(image);if(pendingImageCopies<=0)return;pendingImageCopies--;if(!image)return;
    int w=0,h=0;cef_binary_value_t* png=image->get_as_png(image,1.0f,1,&w,&h);if(!png)return;
    size_t size=png->get_size(png);
    if(size&&size<=32*1024*1024){
      NSMutableData* data=[NSMutableData dataWithLength:size];png->get_data(png,data.mutableBytes,size,0);
      NSPasteboard* board=NSPasteboard.generalPasteboard;[board clearContents];[board setData:data forType:NSPasteboardTypePNG];
    }
    png->base.release(&png->base);
  };
}
static void openInTab(Tab* tab,const cef_string_t* value,bool background){
  NSString* url=ns(value);
  // The new Zen tab starts its own Chromium target in this same profile. It is
  // never a native window, and non-web schemes remain denied.
  if(webBrowsing&&allowedURL(url)&&![url isEqual:@"about:blank"])tabEvent(tab,@"open_url",@{@"url":url,@"background":@(background)});
  else tabEvent(tab,@"error",@{@"code":@"popup_denied"});
}
// A pop-up without a user gesture never opens; Zen shows Firefox's pop-up blocked bar.
static void blockedPopup(Tab* tab,const cef_string_t* value){
  NSString* url=ns(value);
  if(webBrowsing&&url.length<=2048&&allowedURL(url)&&![url isEqual:@"about:blank"])tabEvent(tab,@"popup_blocked",@{@"url":url});
  else tabEvent(tab,@"error",@{@"code":@"popup_denied"});
}
#include "input.inc"
#include "a11y.inc"
static void configureStream(){
  render.api.get_view_rect=[](cef_render_handler_t*,cef_browser_t* value,cef_rect_t* r){HOLD(value);Tab* tab=tabFor(value,true);*r={0,0,tab?tab->width:900,tab?tab->height:650};};
  render.api.get_screen_info=[](cef_render_handler_t*,cef_browser_t* value,cef_screen_info_t* i){
    HOLD(value);Tab* tab=tabFor(value,true);int w=tab?tab->width:900,h=tab?tab->height:650;
    i->device_scale_factor=tab?tab->scale:2;i->depth=32;i->depth_per_component=8;i->rect={0,0,w,h};i->available_rect=i->rect;return 1;
  };
  render.api.on_paint=[](cef_render_handler_t*,cef_browser_t* value,cef_paint_element_type_t type,size_t,const cef_rect_t*,const void* pixels,int w,int h){HOLD(value);if(Tab* tab=tabFor(value))streamSurfacePaint(tab,type,pixels,w,h);};
  render.api.on_accelerated_paint=[](cef_render_handler_t*,cef_browser_t* value,cef_paint_element_type_t type,size_t count,const cef_rect_t* rects,const cef_accelerated_paint_info_t* info){
    HOLD(value);if(Tab* tab=tabFor(value))streamAcceleratedPaint(tab,type,count,rects,info);
  };
  render.api.on_popup_size=[](cef_render_handler_t*,cef_browser_t* value,const cef_rect_t* rect){HOLD(value);if(Tab* tab=tabFor(value))tab->popupRect=*rect;};
  render.api.on_popup_show=[](cef_render_handler_t*,cef_browser_t* value,int show){
    HOLD(value);Tab* tab=tabFor(value);if(!tab)return;
    clearPopup(tab);tab->popupVisible=show;axio::surface::setPopupVisible(tab->surface,show);invalidateView(value);
  };
  display.api.on_cursor_change=[](cef_display_handler_t*,cef_browser_t* value,cef_cursor_handle_t,cef_cursor_type_t type,const cef_cursor_info_t*){
    HOLD(value);if(Tab* tab=tabFor(value))tabEvent(tab,@"cursor",@{@"cursor":cursorName(type)});
    return 1;
  };
  loading.api.on_load_start=[](cef_load_handler_t*,cef_browser_t* value,cef_frame_t* frame,cef_transition_type_t){
    HOLD(value);HOLD(frame);Tab* tab=tabFor(value);if(!tab||!frame->is_main(frame))return;NSString* url=requestString(frame->get_url(frame));
    tab->historyURL=tab->historyNavigation&&tab->navigationRequest&&[tab->committedURLs containsObject:url]?url:nil;
    tab->documentFrame=requestString(frame->get_identifier(frame));
    tabEvent(tab,@"url",@{@"url":url});
    resetInput(tab); // a new document: the old editable and touch sequence end with it
  };
  lifespan.api.on_after_created=[](cef_life_span_handler_t*,cef_browser_t* value){
    HOLD(value);Tab* tab=creating;creating=nullptr;
    if(!tab){auto* h=value->get_host(value);h->close_browser(h,1);h->base.release(&h->base);return;}
    tab->browser=value;value->base.add_ref(&value->base);tabsByBrowser[value->get_identifier(value)]=tab;
    NSMutableDictionary* full=[tab->target mutableCopy];full[@"engine"]=@"chromium";full[@"native_target_id"]=[NSString stringWithFormat:@"%d",value->get_identifier(value)];tab->target=[full copy];
    tabEvent(tab,@"created",@{@"request_id":tab->createRequest});complete(tab->createRequest,@"success");tab->createRequest=nil;
    if(streamClosing||tab->closing)closeTab(tab);
  };
  lifespan.api.on_before_close=[](cef_life_span_handler_t*,cef_browser_t* value){
    HOLD(value);Tab* tab=tabFor(value);if(!tab||tab==creating)return;
    tab->paintAllowed=false;transport.invalidatePendingFrame(keyOf(tab->key));
    closePromptsFor(tab,@"closed",false);cancelDownloadsFor(tab);finishKeyWaits(tab);axTabClosed(tab);tabEvent(tab,@"closed",@{});
    if(tab->closeRequest){complete(tab->closeRequest,@"success");tab->closeRequest=nil;}
    if(tab->navigationRequest){complete(tab->navigationRequest,@"failed");tab->navigationRequest=nil;}
    tabsByBrowser.erase(value->get_identifier(value));tabsByKey.erase(keyOf(tab->key));
    tab->browser->base.release(&tab->browser->base);axio::surface::destroyTarget(tab->surface);delete tab;
    // Fixture probes own one target: its close ends the host. A web host lives
    // until shutdown or its authenticated pipe closes.
    if(streamIdle()&&(streamClosing||!webBrowsing))closed=true;
  };
  lifespan.api.on_before_popup=[](cef_life_span_handler_t*,cef_browser_t* value,cef_frame_t* frame,int,const cef_string_t* url,const cef_string_t*,cef_window_open_disposition_t disposition,int gesture,const cef_popup_features_t*,cef_window_info_t*,cef_client_t**,cef_browser_settings_t*,cef_dictionary_value_t**,int*){
    HOLD(value);HOLD(frame);Tab* tab=tabFor(value);if(!tab)return 1;
    if(gesture)openInTab(tab,url,disposition==CEF_WOD_NEW_BACKGROUND_TAB);
    else blockedPopup(tab,url);
    return 1;
  };
  request.api.on_before_browse=[](cef_request_handler_t*,cef_browser_t* value,cef_frame_t* frame,cef_request_t* candidate,int,int){
    HOLD(value);HOLD(frame);HOLD(candidate);Tab* tab=tabFor(value);if(!tab)return 1;
    NSString* url=requestString(candidate->get_url(candidate)),*method=requestString(candidate->get_method(candidate));
    if(!allowedURL(url,!frame->is_main(frame))||(!webBrowsing&&![method isEqual:@"GET"])){tabEvent(tab,@"error",@{@"code":@"navigation_denied"});return 1;}
    if(frame->is_main(frame)){
      tab->sameDocumentNavigation=tab->sameDocumentNavigation||fragmentNavigation(tab,url);
      if(!tab->commandNavigationInvalidated)invalidateNavigation(tab);
    }return transport.failed()?1:0;
  };
  request.api.on_open_urlfrom_tab=[](cef_request_handler_t*,cef_browser_t* value,cef_frame_t* frame,const cef_string_t* url,cef_window_open_disposition_t disposition,int gesture){
    HOLD(value);HOLD(frame);Tab* tab=tabFor(value);if(!tab)return 1;
    if(webBrowsing&&disposition==CEF_WOD_CURRENT_TAB&&allowedURL(ns(url)))return 0;
    if(gesture&&(disposition==CEF_WOD_NEW_FOREGROUND_TAB||disposition==CEF_WOD_NEW_BACKGROUND_TAB))openInTab(tab,url,disposition==CEF_WOD_NEW_BACKGROUND_TAB);
    else if(!gesture&&disposition!=CEF_WOD_CURRENT_TAB)blockedPopup(tab,url);
    else tabEvent(tab,@"error",@{@"code":@"popup_denied"});
    return 1;
  };
  request.api.get_resource_request_handler=[](cef_request_handler_t*,cef_browser_t* value,cef_frame_t* frame,cef_request_t* candidate,int,int,const cef_string_t*,int*){
    HOLD(value);HOLD(frame);HOLD(candidate);return resources.retain();
  };
  resources.api.on_before_resource_load=[](cef_resource_request_handler_t*,cef_browser_t* value,cef_frame_t* frame,cef_request_t* candidate,cef_callback_t* callback){
    HOLD(value);HOLD(frame);HOLD(candidate);HOLD(callback);
    @autoreleasepool{NSString* url=requestString(candidate->get_url(candidate)),*method=requestString(candidate->get_method(candidate));return allowedURL(url,true)&&(webBrowsing||[method isEqual:@"GET"])?RV_CONTINUE:RV_CANCEL;}
  };
  resources.api.on_protocol_execution=[](cef_resource_request_handler_t*,cef_browser_t* value,cef_frame_t* frame,cef_request_t* candidate,int* allow){HOLD(value);HOLD(frame);HOLD(candidate);*allow=0;};
  request.api.on_certificate_error=[](cef_request_handler_t*,cef_browser_t* value,cef_errorcode_t code,const cef_string_t*,cef_sslinfo_t* info,cef_callback_t* callback){
    HOLD(value);HOLD(info);HOLD(callback);
    if(Tab* tab=tabFor(value))tabEvent(tab,@"error",@{@"code":@"certificate_error",@"native_code":@(code)});
    return 0;
  };
  // Credentials are asked for by Zen's own auth prompt and never logged. CEF calls
  // this on the IO thread; all prompt state is UI-thread owned, so hop to main.
  request.api.get_auth_credentials=[](cef_request_handler_t*,cef_browser_t* value,const cef_string_t* origin_,int proxy,const cef_string_t* host_,int port,const cef_string_t* realm_,const cef_string_t* scheme_,cef_auth_callback_t* callback)->int{
    HOLD(value);HOLD(callback);
    if(!webBrowsing||!value||!callback)return 0;
    @autoreleasepool{
      int browserId=value->get_identifier(value);
      NSString *origin=ns(origin_),*host=ns(host_),*realm=clip(ns(realm_),150),*scheme=clip(ns(scheme_),32);
      if(!host.length||host.length>255||port<0||port>65535)return 0;
      callback->base.add_ref(&callback->base);
      dispatch_async(dispatch_get_main_queue(),^{
        auto found=tabsByBrowser.find(browserId);Tab* tab=found==tabsByBrowser.end()?nullptr:found->second;
        NSDictionary* details=@{@"origin":wholeURL(origin,2048),@"host":host,@"port":@(port),@"is_proxy":@(bool(proxy)),@"realm":realm,@"scheme":scheme};
        if(!issuePrompt(tab,PromptAuth,&callback->base,details,300))callback->cancel(callback);
        callback->base.release(&callback->base);
      });
      return 1;
    }
  };
  request.api.on_select_client_certificate=[](cef_request_handler_t*,cef_browser_t* value,int,const cef_string_t*,int,size_t count,cef_x509_certificate_t* const* certificates,cef_select_client_certificate_callback_t* callback){
    HOLD(value);HOLD(callback);
    for(size_t i=0;i<count;i++)if(certificates[i])certificates[i]->base.release(&certificates[i]->base);
    if(Tab* tab=tabFor(value))tabEvent(tab,@"error",@{@"code":@"client_certificate_unavailable"});
    callback->select(callback,nullptr);return 1;
  };
  // getUserMedia/getDisplayMedia: denied with an explicit notice (see permissionNames).
  permissions.api.on_request_media_access_permission=[](cef_permission_handler_t*,cef_browser_t* value,cef_frame_t* frame,const cef_string_t*,uint32_t,cef_media_access_callback_t* callback){
    HOLD(value);HOLD(frame);HOLD(callback);
    if(Tab* tab=tabFor(value))tabEvent(tab,@"error",@{@"code":webBrowsing?@"media_capture_unavailable":@"permission_denied"});
    callback->cancel(callback);return 1;
  };
  permissions.api.on_show_permission_prompt=[](cef_permission_handler_t*,cef_browser_t* value,uint64_t nativePrompt,const cef_string_t* origin_,uint32_t requested,cef_permission_prompt_callback_t* callback){
    HOLD(value);HOLD(callback);Tab* tab=tabFor(value);
    bool capture=false;NSArray* names=permissionNames(requested,&capture);NSString* origin=ns(origin_);
    if(webBrowsing&&tab&&names&&webOrigin(origin)){
      if(Prompt* p=issuePrompt(tab,PromptPermission,&callback->base,@{@"origin":origin,@"permissions":names},300)){p->nativePrompt=nativePrompt;return 1;}
    }
    if(tab)tabEvent(tab,@"error",@{@"code":capture&&webBrowsing?@"media_capture_unavailable":@"permission_denied"});
    callback->cont(callback,CEF_PERMISSION_RESULT_DENY);return 1;
  };
  permissions.api.on_dismiss_permission_prompt=[](cef_permission_handler_t*,cef_browser_t* value,uint64_t nativePrompt,cef_permission_request_result_t){
    HOLD(value);
    // Chromium withdrew a still-open request (navigation, closure): close Zen's doorhanger.
    for(auto& item:prompts)if(item.second->kind==PromptPermission&&item.second->nativePrompt==nativePrompt){closePrompt(item.second,@"withdrawn",false);break;}
  };
  client.api.get_download_handler=[](cef_client_t*){return downloads.retain();};
  downloads.api.can_download=[](cef_download_handler_t*,cef_browser_t* value,const cef_string_t* url,const cef_string_t*){
    HOLD(value);Tab* tab=tabFor(value);
    if(webBrowsing&&tab&&allowedURL(ns(url),true))return 1;
    if(tab)tabEvent(tab,@"error",@{@"code":@"download_denied"});
    return 0;
  };
  downloads.api.on_before_download=[](cef_download_handler_t*,cef_browser_t* value,cef_download_item_t* item,const cef_string_t* suggested,cef_before_download_callback_t* callback)->int{
    HOLD(value);HOLD(item);HOLD(callback);Tab* tab=tabFor(value);
    if(!webBrowsing||!tab)return 0;
    uint32_t id=item->get_id(item);
    if(activeDownloads.count(id)){return 0;}
    NSString* name=[[ns(suggested) lastPathComponent] stringByReplacingOccurrencesOfString:@":" withString:@"_"];
    NSDictionary* details=@{@"download_id":@(id),@"suggested_name":clip(name.length?name:@"download",255),@"url":wholeURL(requestString(item->get_url(item)),2048),
      @"mime_type":clip(requestString(item->get_mime_type(item)),128),@"total_bytes":@(std::max<int64_t>(-1,item->get_total_bytes(item)))};
    activeDownloads[id]=ActiveDownload{tab,false,nullptr};
    // Where to save is Zen's decision; the download survives page navigation.
    Prompt* p=issuePrompt(tab,PromptDownload,&callback->base,details,600,false);
    if(!p){activeDownloads.erase(id);return 0;}
    p->download=id;return 1;
  };
  downloads.api.on_download_updated=[](cef_download_handler_t*,cef_browser_t* value,cef_download_item_t* item,cef_download_item_callback_t* callback){
    HOLD(value);HOLD(item);HOLD(callback);Tab* tab=tabFor(value);
    if(!webBrowsing){callback->cancel(callback);return;}
    auto found=activeDownloads.find(item->get_id(item));
    // Updates can precede on_before_download; until Zen accepts a save location
    // nothing is written, and on_before_download alone decides whether it may start.
    if(found==activeDownloads.end()||!found->second.accepted)return;
    if(!tab||found->second.tab!=tab){callback->cancel(callback);return;}
    if(found->second.control)found->second.control->base.release(&found->second.control->base);
    callback->base.add_ref(&callback->base);found->second.control=callback;
    NSString* state=item->is_complete(item)?@"complete":(item->is_canceled(item)?@"canceled":(item->is_interrupted(item)?@"interrupted":@"in_progress"));
    tabEvent(tab,@"download_updated",@{@"download_id":@(found->first),@"state":state,@"received_bytes":@(std::max<int64_t>(0,item->get_received_bytes(item))),
      @"total_bytes":@(std::max<int64_t>(-1,item->get_total_bytes(item))),@"speed":@(std::max<int64_t>(0,item->get_current_speed(item))),@"paused":@(bool(item->is_paused(item)))});
    if(![state isEqual:@"in_progress"]){found->second.control->base.release(&found->second.control->base);activeDownloads.erase(found);}
  };
  client.api.get_context_menu_handler=[](cef_client_t*){return contextMenu.retain();};
  contextMenu.api.on_before_context_menu=[](cef_context_menu_handler_t*,cef_browser_t* value,cef_frame_t* frame,cef_context_menu_params_t* params,cef_menu_model_t* model){
    HOLD(value);HOLD(frame);HOLD(params);HOLD(model);
    // Fixture sessions show no menu. Web sessions keep the model so CEF asks
    // run_context_menu, where Zen draws Firefox's own menu instead.
    if(!webBrowsing){model->clear(model);return;}
    // CEF skips run_context_menu for an empty model (e.g. links/images in Alloy).
    // This inert item is never shown or executed; Zen builds the real menu.
    if(!model->get_count(model)){auto label=utf16(@"AxioSozo");auto text=view(label);model->add_item(model,MENU_ID_USER_FIRST,&text);}
  };
  contextMenu.api.run_context_menu=[](cef_context_menu_handler_t*,cef_browser_t* value,cef_frame_t* frame,cef_context_menu_params_t* params,cef_menu_model_t* model,cef_run_context_menu_callback_t* callback)->int{
    HOLD(value);HOLD(frame);HOLD(params);HOLD(model);HOLD(callback);Tab* tab=tabFor(value);
    if(!webBrowsing||!tab){callback->cancel(callback);return 1;}
    NSString *link=wholeURL(requestString(params->get_link_url(params)),2048),*source=wholeURL(requestString(params->get_source_url(params)),2048);
    NSString *frameURL=wholeURL(requestString(params->get_frame_url(params)),1024),*misspelled=clip(requestString(params->get_misspelled_word(params)),64);
    loadStringLists();cef_string_list_t list=p_list_alloc();params->get_dictionary_suggestions(params,list);
    NSMutableArray* suggestions=[NSMutableArray array];for(NSString* word in listStrings(list,5))if(word.length&&word.length<=64)[suggestions addObject:word];
    p_list_free(list);
    int media=params->get_media_type(params);
    NSDictionary* details=@{@"x":@(std::max(0,params->get_xcoord(params))),@"y":@(std::max(0,params->get_ycoord(params))),
      @"type_flags":@(int(params->get_type_flags(params))&63),@"link_url":link,@"source_url":source,@"frame_url":frameURL,
      @"selection_text":clip(requestString(params->get_selection_text(params)),256),@"link_text":clip(requestString(params->get_title_text(params)),128),
      @"editable":@(bool(params->is_editable(params))),@"edit_flags":@(int(params->get_edit_state_flags(params))&511),
      @"media_type":mediaName(media),@"media_flags":@(int(params->get_media_state_flags(params))&8191),
      @"misspelled_word":misspelled,@"suggestions":suggestions,@"spellcheck":@(bool(params->is_spell_check_enabled(params)))};
    Prompt* p=issuePrompt(tab,PromptContextMenu,&callback->base,details,120);
    if(!p){callback->cancel(callback);return 1;}
    p->frame=frame;frame->base.add_ref(&frame->base);p->link=link;p->source=source;p->misspelled=misspelled;p->suggestions=suggestions;p->mediaType=media;
    return 1;
  };
  client.api.get_dialog_handler=[](cef_client_t*){return dialogs.retain();};
  dialogs.api.on_file_dialog=[](cef_dialog_handler_t*,cef_browser_t* value,cef_file_dialog_mode_t mode,const cef_string_t* title,const cef_string_t* defaultPath,cef_string_list_t filters_,cef_string_list_t extensions_,cef_string_list_t descriptions_,cef_file_dialog_callback_t* callback){
    HOLD(value);HOLD(callback);Tab* tab=tabFor(value);
    NSString* modeName=mode==FILE_DIALOG_OPEN?@"open":(mode==FILE_DIALOG_OPEN_MULTIPLE?@"open_multiple":(mode==FILE_DIALOG_OPEN_FOLDER?@"open_folder":(mode==FILE_DIALOG_SAVE?@"save":nil)));
    if(webBrowsing&&tab&&modeName){
      NSArray *filters=listStrings(filters_,16),*extensions=listStrings(extensions_,16),*descriptions=listStrings(descriptions_,16);
      NSMutableArray* accept=[NSMutableArray array];
      for(NSUInteger i=0;i<filters.count;i++){
        NSMutableArray* list=[NSMutableArray array];
        NSString* joined=i<extensions.count&&[extensions[i] length]?extensions[i]:filters[i];
        for(NSString* part in [joined componentsSeparatedByString:@";"])if([part hasPrefix:@"."]&&part.length<=16&&list.count<16)[list addObject:part];
        [accept addObject:@{@"filter":clip(filters[i],64),@"extensions":list,@"description":clip(i<descriptions.count?descriptions[i]:@"",64)}];
      }
      // Only the suggested file name reaches Zen; the picker opens in Zen's own directory.
      NSDictionary* details=@{@"mode":modeName,@"title":clip(ns(title),128),@"default_name":clip([ns(defaultPath) lastPathComponent],255),@"filters":accept};
      if(Prompt* p=issuePrompt(tab,PromptFile,&callback->base,details,600)){p->fileMode=mode;return 1;}
    }
    if(tab)tabEvent(tab,@"error",@{@"code":@"file_dialog_unavailable"});
    callback->cancel(callback);return 1;
  };
  client.api.get_jsdialog_handler=[](cef_client_t*){return jsDialogs.retain();};
  jsDialogs.api.on_jsdialog=[](cef_jsdialog_handler_t*,cef_browser_t* value,const cef_string_t* origin,cef_jsdialog_type_t type,const cef_string_t* message,const cef_string_t* defaultText,cef_jsdialog_callback_t* callback,int* suppress)->int{
    HOLD(value);HOLD(callback);Tab* tab=tabFor(value);
    NSString* typeName=type==JSDIALOGTYPE_ALERT?@"alert":(type==JSDIALOGTYPE_CONFIRM?@"confirm":(type==JSDIALOGTYPE_PROMPT?@"prompt":nil));
    if(webBrowsing&&tab&&typeName){
      NSDictionary* details=@{@"dialog_type":typeName,@"origin_url":wholeURL(ns(origin),2048),@"message":clip(ns(message),3000),@"default_text":clip(ns(defaultText),1024)};
      if(issuePrompt(tab,PromptDialog,&callback->base,details,300))return 1;
    }
    if(tab)tabEvent(tab,@"error",@{@"code":@"javascript_dialog_unavailable"});
    *suppress=1;return 0;
  };
  jsDialogs.api.on_before_unload_dialog=[](cef_jsdialog_handler_t*,cef_browser_t* value,const cef_string_t*,int reload,cef_jsdialog_callback_t* callback){
    HOLD(value);HOLD(callback);Tab* tab=tabFor(value);bool leave=streamClosing||(tab&&tab->closing);
    // Closing the tab or the host never waits for the page. Pages cannot supply the text.
    if(!leave&&tab&&issuePrompt(tab,PromptBeforeUnload,&callback->base,@{@"dialog_type":@"beforeunload",@"is_reload":@(bool(reload))},300))return 1;
    if(tab&&!leave)tabEvent(tab,@"error",@{@"code":@"before_unload_confirmation_unavailable"});
    callback->cont(callback,leave?1:0,nullptr);if(tab&&!leave)restoreCancelledDocument(tab,value);return 1;
  };
  jsDialogs.api.on_reset_dialog_state=[](cef_jsdialog_handler_t*,cef_browser_t* value){
    HOLD(value);Tab* tab=tabFor(value);if(!tab)return;
    std::vector<uint64_t> matched;
    for(auto& item:prompts)if(item.second->tab==tab&&(item.second->kind==PromptDialog||item.second->kind==PromptBeforeUnload))matched.push_back(item.first);
    // Chromium already abandoned these dialogs (e.g. navigation); answer "cancel"
    // without treating it as the user choosing to stay on the page. A prompt that
    // is being answered right now was detached first and is not touched here.
    for(uint64_t id:matched){
      Prompt* p=detachPrompt(id);if(!p)continue;
      tabEvent(tab,@"prompt_closed",@{@"prompt_id":promptName(p->id),@"reason":@"reset"});
      auto* c=(cef_jsdialog_callback_t*)p->callback;c->cont(c,0,nullptr);releasePrompt(p);
    }
  };
  configureDelegation();
  configureInput();
  display.api.on_title_change=[](cef_display_handler_t*,cef_browser_t* value,const cef_string_t* title_){
    HOLD(value);Tab* tab=tabFor(value);if(!tab)return;NSString* title=ns(title_);
    tabEvent(tab,@"title",@{@"title":[title substringToIndex:MIN(title.length,1024)]});
  };
  display.api.on_favicon_urlchange=[](cef_display_handler_t*,cef_browser_t* value,cef_string_list_t icons){
    HOLD(value);Tab* tab=tabFor(value);if(!tab||tab->closing)return;
    // Chromium lists the declared icons in document order, else the default /favicon.ico.
    NSString* chosen=nil;
    for(NSString* url in listStrings(icons,32))if(url.length<=4096&&allowedURL(url,true)&&![url isEqual:@"about:blank"]){chosen=url;break;}
    requestIcon(tab,value,chosen);
  };
  display.api.on_address_change=[](cef_display_handler_t*,cef_browser_t* value,cef_frame_t* frame,const cef_string_t* address){
    HOLD(value);HOLD(frame);Tab* tab=tabFor(value);if(!tab||!frame->is_main(frame))return;NSString* url=ns(address);
    // CEF does not send load callbacks for pushState or fragment navigation.
    // Revoke the old target, then explicitly label this already-committed page.
    bool sameDocument=webBrowsing&&allowedURL(url)&&tab->committedDocumentURL&&
      (tab->sameDocumentNavigation||(tab->paintAllowed&&![url isEqual:tab->committedDocumentURL]));
    if(sameDocument){
      // The document (and its RenderFrameHost) is unchanged; only its target is revoked.
      if(!tab->sameDocumentNavigation){NSString* retained=tab->documentFrame;invalidateNavigation(tab);tab->documentFrame=retained;}
      tab->committedDocumentURL=url;tab->paintAllowed=true;
      tabEvent(tab,@"url",@{@"url":url});tabEvent(tab,@"load",@{@"http_status":@0,@"restored_from_history":@NO,@"same_document":@YES});
      if(tab->navigationRequest){complete(tab->navigationRequest,@"success");tab->navigationRequest=nil;}
      resetNavigation(tab);invalidateView(value);
    }else tabEvent(tab,@"url",@{@"url":url});
  };
  loading.api.on_loading_state_change=[](cef_load_handler_t*,cef_browser_t* value,int isLoading,int canBack,int canForward){
    HOLD(value);if(Tab* tab=tabFor(value))tabEvent(tab,@"loading",@{@"loading":@(bool(isLoading)),@"can_go_back":@(bool(canBack)),@"can_go_forward":@(bool(canForward))});
  };
  loading.api.on_load_end=[](cef_load_handler_t*,cef_browser_t* value,cef_frame_t* frame,int status){
    HOLD(value);HOLD(frame);Tab* tab=tabFor(value);if(!tab||!frame->is_main(frame))return;
    // CEF commits (OnLoadStart) before it finishes (OnLoadEnd) every document it
    // loads, BFCache restores included. Error pages commit via OnLoadError only.
    if(!tab->documentFrame||![requestString(frame->get_identifier(frame)) isEqual:tab->documentFrame])return;
    NSString* url=requestString(frame->get_url(frame));
    bool restored=status==0&&tab->historyNavigation&&tab->navigationRequest&&tab->historyURL&&[tab->historyURL isEqual:url]&&[tab->committedURLs containsObject:url]&&allowedURL(url);
    bool committed=(webBrowsing?(status>=200&&status<=599):status==200)&&allowedURL(url);
    tab->paintAllowed=committed||restored||(webBrowsing&&status==0&&[url isEqual:@"about:blank"]);
    if(tab->paintAllowed)tab->committedDocumentURL=url;
    if(committed){if(tab->committedURLs.count>=1024)[tab->committedURLs removeAllObjects];[tab->committedURLs addObject:url];}
    tabEvent(tab,@"load",@{@"http_status":@(status),@"restored_from_history":@(restored)});
    if(tab->navigationRequest){complete(tab->navigationRequest,tab->paintAllowed?@"success":@"failed");tab->navigationRequest=nil;}
    resetNavigation(tab);tab->supersededLoads=0;invalidateView(value);
  };
  loading.api.on_load_error=[](cef_load_handler_t*,cef_browser_t* value,cef_frame_t* frame,cef_errorcode_t code,const cef_string_t*,const cef_string_t* failed){
    HOLD(value);HOLD(frame);Tab* tab=tabFor(value);if(!tab||!frame->is_main(frame))return;
    if(webBrowsing&&code==ERR_ABORTED&&tab->supersededLoads>0&&(!tab->navigateURL||![ns(failed) isEqual:tab->navigateURL])){
      // The replaced load reports its own cancellation; the current request continues.
      tab->supersededLoads--;return;
    }
    if(webBrowsing&&code==ERR_ABORTED){
      // Chromium uses ABORTED for replaced navigations and denied downloads.
      // The replacement's callbacks establish its own success; never label a
      // cancellation as a renderer crash or replay its potentially unsafe POST.
      resetNavigation(tab);
      tabEvent(tab,@"error",@{@"code":@"navigation_cancelled",@"native_code":@(code)});
      if(tab->navigationRequest){unsupported(tab->navigationRequest,@"NAVIGATION_CANCELLED");tab->navigationRequest=nil;}
      restoreCancelledDocument(tab,value);return;
    }
    tab->paintAllowed=false;resetNavigation(tab);tab->committedDocumentURL=nil;tab->documentFrame=nil;tabEvent(tab,@"error",@{@"code":@"load_failed",@"native_code":@(code)});
    if(tab->navigationRequest){complete(tab->navigationRequest,@"failed");tab->navigationRequest=nil;}
  };
  request.api.on_render_process_terminated=[](cef_request_handler_t*,cef_browser_t* value,cef_termination_status_t,int code,const cef_string_t*){
    HOLD(value);Tab* tab=tabFor(value);if(!tab)return;
    invalidateNavigation(tab);resetNavigation(tab);
    tabEvent(tab,@"error",@{@"code":@"render_process_terminated",@"native_code":@(code)});
    if(tab->navigationRequest){complete(tab->navigationRequest,@"failed");tab->navigationRequest=nil;}
    // Recovery is explicit reload/back/switch in Zen; never replay form submits.
  };
}
