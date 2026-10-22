// Included by stream.inc. Keyboard verdicts, IME, trackpad wheel phases, pinch and
// frame-rate renegotiation (contracts/cef-v1.md "Input"). All state is UI-thread
// owned. Nothing here blocks: verdicts complete from CEF callbacks or a timer.
//
// Key verdicts. Chromium acknowledges keyboard events strictly in order per widget
// (RenderWidgetHostImpl::OnKeyboardEventAck) and hands only unconsumed ones to
// CefKeyboardHandler::OnKeyEvent. A consumed keydown produces no callback, so after
// each keydown (and its own `char`) the host sends one inert probe: a KEYEVENT_CHAR
// with is_system_key set. Blink answers such a char kNotHandled before any DOM
// dispatch or suppression check (WebFrameWidgetImpl::HandleCharEvent, Chromium
// 156.0.8078.17, unchanged since 154.0.8037.17; no access-key modifiers, keycode
// not Backspace/Escape), so the page
// never observes it and it always returns through OnKeyEvent. A keydown whose own
// unhandled report precedes its probe is `key_not_consumed`; one whose probe
// arrives first was consumed by the renderer. The probe is deferred until the
// keydown's `char` (or any later command) because a system-key char also clears
// Blink's "suppress next keypress" flag. No answer within KeyVerdictTimeoutMs, or a
// target that closes, completes `key_not_consumed` so Zen's shortcuts still run.
static constexpr int KeyProbeBase=0x7000;  // synthetic NSEvent keyCode; macOS codes are < 0x80
static constexpr uint64_t KeyProbeMask=0xFFF;
static constexpr int64_t KeyProbeDelayMs=30,KeyVerdictTimeoutMs=500;
static constexpr size_t MaxKeyWaits=32,MaxProbesInFlight=64;
static constexpr char16_t KeyProbeCharacter=0xE000;  // private use; never a function key
static constexpr NSUInteger ImeTextLimit=1024,ImeUnderlineLimit=16;
static constexpr int64_t ImeRangeLimit=INT32_MAX,ImeCursorLimit=65536;
static constexpr uint64_t TextInputIntervalNs=16000000;  // at most ~60 text_input events/s per tab
static uint64_t nowNs(){return clock_gettime_nsec_np(CLOCK_UPTIME_RAW);}
static void completeKey(NSString* request,bool consumed){
  event(@"completed",@{@"request_id":request,@"status":@"success",@"reason":consumed?@"key_consumed":@"key_not_consumed"});
}
// Resolve every waiting keydown up to and including `seq`.
static void resolveKeysThrough(Tab* tab,uint64_t seq,bool consumed){
  while(!tab->keyWaits.empty()&&tab->keyWaits.front().seq<=seq){
    NSString* request=tab->keyWaits.front().request;tab->keyWaits.pop_front();completeKey(request,consumed);
  }
}
static void finishKeyWaits(Tab* tab){
  while(!tab->keyWaits.empty()){NSString* request=tab->keyWaits.front().request;tab->keyWaits.pop_front();completeKey(request,false);}
  tab->probesInFlight.clear();tab->probeSent=tab->keySeq;
}
static void sendKeyProbe(Tab* tab){
  if(tab->probeSent>=tab->keySeq)return;
  uint64_t seq=tab->keySeq;tab->probeSent=seq;
  if(!tab->browser||tab->closing)return;
  // An open <select>/date popup receives chars itself (HandleCharEvent's page
  // popup branch); without a probe the verdict comes from OnKeyEvent or the timer.
  if(tab->popupVisible)return;
  cef_key_event_t probe{};probe.size=sizeof(probe);probe.type=KEYEVENT_CHAR;probe.is_system_key=1;probe.modifiers=0;
  probe.native_key_code=KeyProbeBase+int(seq&KeyProbeMask);probe.character=KeyProbeCharacter;probe.unmodified_character=KeyProbeCharacter;
  if(tab->probesInFlight.size()>=MaxProbesInFlight)tab->probesInFlight.pop_front();
  tab->probesInFlight.push_back(seq);
  auto* host=tab->browser->get_host(tab->browser);host->send_key_event(host,&probe);host->base.release(&host->base);
}
static void scheduleKeyTimers(Tab* tab,uint64_t seq){
  int browserId=tab->browser->get_identifier(tab->browser);
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW,KeyProbeDelayMs*int64_t(NSEC_PER_MSEC)),dispatch_get_main_queue(),^{
    Tab* current=tabForId(uint64_t(browserId));if(current&&current->keySeq==seq)sendKeyProbe(current);
  });
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW,KeyVerdictTimeoutMs*int64_t(NSEC_PER_MSEC)),dispatch_get_main_queue(),^{
    Tab* current=tabForId(uint64_t(browserId));if(!current)return;
    for(auto it=current->keyWaits.begin();it!=current->keyWaits.end();++it)
      if(it->seq==seq){NSString* request=it->request;current->keyWaits.erase(it);completeKey(request,false);return;}
  });
}
// CefKeyboardHandler::OnKeyEvent: only events the renderer did not consume.
static void keyReport(Tab* tab,const cef_key_event_t* key){
  if(key->type==KEYEVENT_CHAR&&key->is_system_key){
    int code=key->native_key_code-KeyProbeBase;
    if(code<0||code>int(KeyProbeMask))return;
    while(!tab->probesInFlight.empty()){
      uint64_t seq=tab->probesInFlight.front();tab->probesInFlight.pop_front();
      if((seq&KeyProbeMask)!=uint64_t(code))continue;  // an earlier probe was lost (focus moved widget)
      tab->probeArrived=std::max(tab->probeArrived,seq);
      resolveKeysThrough(tab,seq,true);  // their unhandled reports would have come first
      return;
    }
    return;
  }
  if(key->type!=KEYEVENT_RAWKEYDOWN||key->is_system_key)return;
  // The report belongs to the oldest keydown whose probe has not returned; the
  // native key code must match what that request sent.
  for(auto& wait:tab->keyWaits){
    if(wait.seq<=tab->probeArrived)continue;
    if(wait.nativeKeyCode!=key->native_key_code)continue;
    uint64_t seq=wait.seq;
    if(seq>0)resolveKeysThrough(tab,seq-1,true);
    NSString* request=tab->keyWaits.front().request;tab->keyWaits.pop_front();completeKey(request,false);
    return;
  }
}
// CEF's macOS translator (TranslateWebKeyEvent) rebuilds an NSEvent and treats
// character == unmodified_character == 0 as NSEventTypeFlagsChanged, which Chromium
// turns into a RawKeyDown whatever the requested type. Only modifier keys may do
// that. For non-text keys the host supplies the character AppKit itself reports
// for [NSEvent characters], as cefclient forwards it (e.g. NSLeftArrowFunctionKey).
static char16_t appKitCharacter(int code){
  switch(code){
    case 0x24:return 0x0D; case 0x4C:return 0x03; case 0x30:return 0x09; case 0x31:return 0x20;
    case 0x33:return 0x7F; case 0x35:return 0x1B; case 0x47:return 0xF739; case 0x72:return 0xF746;
    case 0x73:return 0xF729; case 0x74:return 0xF72C; case 0x75:return 0xF728; case 0x77:return 0xF72B; case 0x79:return 0xF72D;
    case 0x7B:return 0xF702; case 0x7C:return 0xF703; case 0x7D:return 0xF701; case 0x7E:return 0xF700;
    case 0x7A:return 0xF704; case 0x78:return 0xF705; case 0x63:return 0xF706; case 0x76:return 0xF707;
    case 0x60:return 0xF708; case 0x61:return 0xF709; case 0x62:return 0xF70A; case 0x64:return 0xF70B;
    case 0x65:return 0xF70C; case 0x6D:return 0xF70D; case 0x67:return 0xF70E; case 0x6F:return 0xF70F;
    case 0x69:return 0xF710; case 0x6B:return 0xF711; case 0x71:return 0xF712; case 0x6A:return 0xF713;
    case 0x40:return 0xF714; case 0x4F:return 0xF715; case 0x50:return 0xF716; case 0x5A:return 0xF717;
    default:return 0;  // modifiers (0x36-0x3F) and unknown codes: flags-changed, as before
  }
}
static bool keyCommand(Tab* tab,NSDictionary* command,NSString* requestId){
  NSString* type=command[@"type"],*text=command[@"text"];
  if(![@[@"down",@"up",@"char"] containsObject:type]||!integer(command[@"native_key_code"],65535)||!integer(command[@"windows_key_code"],255)||!integer(command[@"modifiers"],131071)||!textValue(text,4)){failProtocol();return true;}
  event(@"accepted",@{@"request_id":requestId});
  cef_key_event_t key{};key.size=sizeof(key);
  key.type=[type isEqual:@"char"]?KEYEVENT_CHAR:([type isEqual:@"down"]?KEYEVENT_RAWKEYDOWN:KEYEVENT_KEYUP);
  key.native_key_code=[command[@"native_key_code"] intValue];key.windows_key_code=[command[@"windows_key_code"] intValue];key.modifiers=[command[@"modifiers"] unsignedIntValue];
  auto* host=tab->browser->get_host(tab->browser);
  if(key.type==KEYEVENT_CHAR){for(NSUInteger i=0;i<text.length;i++){key.character=[text characterAtIndex:i];key.unmodified_character=key.character;host->send_key_event(host,&key);}}
  else {
    key.character=text.length?[text characterAtIndex:0]:appKitCharacter(key.native_key_code);key.unmodified_character=key.character;
    host->send_key_event(host,&key);
  }
  host->base.release(&host->base);
  if(key.type==KEYEVENT_CHAR){complete(requestId,@"success");sendKeyProbe(tab);return true;}
  if(key.type!=KEYEVENT_RAWKEYDOWN){complete(requestId,@"success");return true;}
  if(tab->keyWaits.size()>=MaxKeyWaits){  // a stalled renderer: oldest verdict defaults
    NSString* oldest=tab->keyWaits.front().request;tab->keyWaits.pop_front();completeKey(oldest,false);
  }
  uint64_t seq=++tab->keySeq;
  tab->keyWaits.push_back(Tab::KeyWait{seq,requestId,key.native_key_code});
  scheduleKeyTimers(tab,seq);
  return true;
}

// ---- Text input (IME proxy placement) ------------------------------------------
// The renderer helper reports focused-node changes (editable, password, element
// bounds) with one bounded process message, as cefclient's client_renderer does.
// The renderer is untrusted: the message is exact-schema checked, clamped to the
// view and coalesced. During composition Chromium's character bounds (view DIP,
// OnImeCompositionRangeChanged) refine the caret.
static NSString* textModeName(int mode){return mode==2?@"password":(mode==1?@"text":@"none");}
static void emitTextInput(Tab* tab){
  tab->textInputScheduled=false;
  if(!tab->target[@"native_target_id"]||tab->closing||streamClosing)return;
  if(tab->sentMode==tab->textMode&&!memcmp(tab->sentCaret,tab->caret,sizeof(tab->caret)))return;
  tab->sentMode=tab->textMode;memcpy(tab->sentCaret,tab->caret,sizeof(tab->caret));tab->textInputAt=nowNs();
  tabEvent(tab,@"text_input",@{@"mode":textModeName(tab->textMode),@"caret_x":@(tab->caret[0]),@"caret_y":@(tab->caret[1]),
    @"caret_width":@(tab->caret[2]),@"caret_height":@(tab->caret[3])});
}
static void setTextInput(Tab* tab,int mode,double x,double y,double w,double h){
  auto clampTo=[](double v,double hi){return std::isfinite(v)?std::round(std::min(std::max(v,0.0),hi)*100)/100:0.0;};
  double width=tab->width,height=tab->height;
  tab->textMode=mode;
  if(mode){tab->caret[0]=clampTo(x,width);tab->caret[1]=clampTo(y,height);tab->caret[2]=clampTo(w,width);tab->caret[3]=clampTo(h,height);}
  else std::fill(std::begin(tab->caret),std::end(tab->caret),0.0);
  if(tab->textInputScheduled)return;
  uint64_t since=nowNs()-tab->textInputAt;
  if(since>=TextInputIntervalNs){emitTextInput(tab);return;}
  tab->textInputScheduled=true;int browserId=tab->browser?tab->browser->get_identifier(tab->browser):0;
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW,int64_t(TextInputIntervalNs-since)),dispatch_get_main_queue(),^{
    if(Tab* current=tabForId(uint64_t(browserId)))emitTextInput(current);
  });
}
static bool validRange(id value){
  if(![value isKindOfClass:NSDictionary.class]||!keys(value,@[@"start",@"end"]))return false;
  return integer(value[@"start"],ImeRangeLimit)&&integer(value[@"end"],ImeRangeLimit)&&[value[@"start"] longLongValue]<=[value[@"end"] longLongValue];
}
static cef_range_t rangeOf(id value){
  // CefRange::InvalidRange(): Chromium uses the current composition or selection.
  if(!value)return cef_range_t{UINT32_MAX,UINT32_MAX};
  return cef_range_t{uint32_t([value[@"start"] unsignedIntValue]),uint32_t([value[@"end"] unsignedIntValue])};
}
static bool imeCommand(Tab* tab,NSString* method,NSDictionary* command,NSString* requestId){
  NSString* text=command[@"text"];
  if(command[@"text"]&&!textValue(text,ImeTextLimit)){failProtocol();return true;}
  NSUInteger length=text.length;
  if(command[@"replacement_range"]&&!validRange(command[@"replacement_range"])){failProtocol();return true;}
  std::vector<cef_composition_underline_t> underlines;
  if([method isEqual:@"ime_set_composition"]){
    // UTF-16 offsets into `text`; the selection is the caret inside the composition.
    if(!integer(command[@"selection_start"],length)||!integer(command[@"selection_end"],length)
        ||[command[@"selection_start"] unsignedIntegerValue]>[command[@"selection_end"] unsignedIntegerValue]){failProtocol();return true;}
    id lines=command[@"underlines"];
    if(lines&&(![lines isKindOfClass:NSArray.class]||[lines count]>ImeUnderlineLimit)){failProtocol();return true;}
    for(id line in (lines?:@[])){
      if(![line isKindOfClass:NSDictionary.class]||!keys(line,@[@"start",@"end",@"thick"])||!integer(line[@"start"],length)||!integer(line[@"end"],length)
          ||[line[@"start"] unsignedIntegerValue]>[line[@"end"] unsignedIntegerValue]||!boolean(line[@"thick"])){failProtocol();return true;}
      cef_composition_underline_t item{};item.size=sizeof(item);
      item.range={uint32_t([line[@"start"] unsignedIntValue]),uint32_t([line[@"end"] unsignedIntValue])};
      item.color=0;item.background_color=0;  // transparent: Blink draws in the text colour
      item.thick=[line[@"thick"] boolValue];item.style=CEF_CUS_SOLID;underlines.push_back(item);
    }
    if(!lines&&length){
      cef_composition_underline_t item{};item.size=sizeof(item);item.range={0,uint32_t(length)};item.style=CEF_CUS_SOLID;underlines.push_back(item);
    }
  }
  if([method isEqual:@"ime_commit_text"]&&command[@"relative_cursor_pos"]&&!signedInteger(command[@"relative_cursor_pos"],-int(ImeCursorLimit),int(ImeCursorLimit))){failProtocol();return true;}
  if([method isEqual:@"ime_finish_composing"]&&command[@"keep_selection"]&&!boolean(command[@"keep_selection"])){failProtocol();return true;}
  event(@"accepted",@{@"request_id":requestId});
  auto* host=tab->browser->get_host(tab->browser);
  auto value=utf16(text?:@"");auto string=view(value);
  if([method isEqual:@"ime_set_composition"]){
    cef_range_t replacement=rangeOf(command[@"replacement_range"]);
    cef_range_t selection{uint32_t([command[@"selection_start"] unsignedIntValue]),uint32_t([command[@"selection_end"] unsignedIntValue])};
    host->ime_set_composition(host,&string,underlines.size(),underlines.empty()?nullptr:underlines.data(),&replacement,&selection);
  }else if([method isEqual:@"ime_commit_text"]){
    cef_range_t replacement=rangeOf(command[@"replacement_range"]);
    host->ime_commit_text(host,&string,&replacement,command[@"relative_cursor_pos"]?[command[@"relative_cursor_pos"] intValue]:0);
  }else if([method isEqual:@"ime_finish_composing"])host->ime_finish_composing_text(host,[command[@"keep_selection"] boolValue]?1:0);
  else host->ime_cancel_composition(host);
  host->base.release(&host->base);
  complete(requestId,@"success");
  return true;
}

// ---- Wheel phases ----------------------------------------------------------------
// The pinned C API has no phase fields and on macOS CEF ignores the precision and
// by-page flags: TranslateWebWheelEvent (browser_platform_delegate_native_mac.mm)
// always sets kScrollByPrecisePixel with kPhaseNone/kPhaseNone, and the OSR view's
// MouseWheelPhaseHandler then synthesizes a phase (Began, Changed/Stationary, and an
// Ended 500 ms after the last event; latching breaks after 10 px of pointer travel,
// a modifier change or a direction change). macOS momentum events therefore reach
// Chromium as further non-momentum scroll updates (no fling curve of its own). The
// host uses the phases to drop zero-delta boundary events (may_begin, stationary,
// ended, cancelled), which would otherwise extend Chromium's latch by 500 ms.
static NSArray* wheelPhases(){return @[@"none",@"may_begin",@"began",@"changed",@"stationary",@"ended",@"cancelled"];}
static bool wheelCommand(Tab* tab,NSDictionary* command,NSString* requestId){
  if(!integer(command[@"x"],tab->width)||!integer(command[@"y"],tab->height)||!integer(command[@"modifiers"],131071)
      ||!signedInteger(command[@"delta_x"],-4096,4096)||!signedInteger(command[@"delta_y"],-4096,4096)){failProtocol();return true;}
  bool phased=command[@"phase"]!=nil;
  if(phased&&(![wheelPhases() containsObject:command[@"phase"]]||![wheelPhases() containsObject:command[@"momentum_phase"]]||!boolean(command[@"precise"]))){failProtocol();return true;}
  event(@"accepted",@{@"request_id":requestId});
  int dx=[command[@"delta_x"] intValue],dy=[command[@"delta_y"] intValue];
  if(dx||dy){
    cef_mouse_event_t mouse{};mouse.x=[command[@"x"] intValue];mouse.y=[command[@"y"] intValue];mouse.modifiers=[command[@"modifiers"] unsignedIntValue];
    auto* host=tab->browser->get_host(tab->browser);host->send_mouse_wheel_event(host,&mouse,dx,dy);host->base.release(&host->base);
  }
  complete(requestId,@"success");
  return true;
}

// ---- Pinch -----------------------------------------------------------------------
// CEF's OSR view has no pinch entry point; its touch path feeds Chromium's gesture
// provider, which turns two moving touch points into GesturePinchBegin/Update/End
// (pinch-zoom of the visual viewport, like Chrome's trackpad pinch; the layout and
// Chromium zoom level are unchanged). The page first receives the touch/pointer
// events and may cancel them, as it can cancel Chrome's synthetic ctrl+wheel. Two
// points straddle the pinch centre horizontally; their span follows the product of
// (1 + magnification). A pinch with no update for 1 s is cancelled.
static constexpr int PinchTouchA=0x41584901,PinchTouchB=0x41584902;
static void pinchTouch(Tab* tab,int id,double x,cef_touch_event_type_t type,uint32_t modifiers){
  cef_touch_event_t touch{};touch.id=id;touch.x=float(x);touch.y=float(tab->pinchY);touch.type=type;
  touch.modifiers=modifiers;touch.pointer_type=CEF_POINTER_TYPE_TOUCH;
  auto* host=tab->browser->get_host(tab->browser);host->send_touch_event(host,&touch);host->base.release(&host->base);
}
static void cancelPinch(Tab* tab){
  if(!tab->pinching)return;
  tab->pinching=false;
  if(!tab->browser||tab->closing)return;
  double half=tab->pinchSpan*tab->pinchScale/2;
  pinchTouch(tab,PinchTouchA,tab->pinchX-half,CEF_TET_CANCELLED,0);pinchTouch(tab,PinchTouchB,tab->pinchX+half,CEF_TET_CANCELLED,0);
}
static void resetInput(Tab* tab){
  cancelPinch(tab);
  if(tab->textMode||tab->sentMode>0)setTextInput(tab,0,0,0,0,0);
}
static bool pinchCommand(Tab* tab,NSDictionary* command,NSString* requestId){
  id magnification=command[@"magnification"];
  if(!integer(command[@"x"],tab->width)||!integer(command[@"y"],tab->height)||!integer(command[@"modifiers"],131071)
      ||![wheelPhases() containsObject:command[@"phase"]]||![magnification isKindOfClass:NSNumber.class]||CFGetTypeID((__bridge CFTypeRef)magnification)==CFBooleanGetTypeID()
      ||!std::isfinite([magnification doubleValue])||fabs([magnification doubleValue])>10){failProtocol();return true;}
  event(@"accepted",@{@"request_id":requestId});
  NSString* phase=command[@"phase"];uint32_t modifiers=[command[@"modifiers"] unsignedIntValue];
  double m=[magnification doubleValue];
  if([phase isEqual:@"ended"]||[phase isEqual:@"cancelled"]){
    if(tab->pinching){
      double half=tab->pinchSpan*tab->pinchScale/2;tab->pinching=false;
      auto type=[phase isEqual:@"ended"]?CEF_TET_RELEASED:CEF_TET_CANCELLED;
      pinchTouch(tab,PinchTouchA,tab->pinchX-half,type,modifiers);pinchTouch(tab,PinchTouchB,tab->pinchX+half,type,modifiers);
    }
    complete(requestId,@"success");return true;
  }
  bool begin=[phase isEqual:@"began"]||[phase isEqual:@"may_begin"];
  if(begin&&tab->pinching)cancelPinch(tab);
  if(!tab->pinching&&(begin||[phase isEqual:@"changed"]||m!=0)){
    // Both points start inside the view and clear Chromium's minimum scaling span.
    double span=std::clamp(std::min(tab->width,tab->height)*0.5,160.0,400.0);
    tab->pinchSpan=span;tab->pinchScale=1;
    tab->pinchX=std::clamp(double([command[@"x"] intValue]),span/2,std::max(span/2,tab->width-span/2));
    tab->pinchY=[command[@"y"] intValue];tab->pinching=true;
    pinchTouch(tab,PinchTouchA,tab->pinchX-span/2,CEF_TET_PRESSED,modifiers);pinchTouch(tab,PinchTouchB,tab->pinchX+span/2,CEF_TET_PRESSED,modifiers);
  }
  if(tab->pinching&&m!=0){
    // The span never drops below Chromium's scaling threshold; zooming stops there.
    tab->pinchScale=std::clamp(tab->pinchScale*(1+m),140.0/tab->pinchSpan,12.0);
    double half=tab->pinchSpan*tab->pinchScale/2;
    pinchTouch(tab,PinchTouchA,tab->pinchX-half,CEF_TET_MOVED,modifiers);pinchTouch(tab,PinchTouchB,tab->pinchX+half,CEF_TET_MOVED,modifiers);
  }
  if(tab->pinching){
    uint64_t at=tab->pinchAt=nowNs();int browserId=tab->browser->get_identifier(tab->browser);
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,int64_t(NSEC_PER_SEC)),dispatch_get_main_queue(),^{
      Tab* current=tabForId(uint64_t(browserId));if(current&&current->pinching&&current->pinchAt==at)cancelPinch(current);
    });
  }
  complete(requestId,@"success");
  return true;
}

// Returns false when `method` is not an input command handled here.
static bool inputCommand(Tab* tab,NSString* method,NSDictionary* command,NSString* requestId){
  if([method isEqual:@"key"])return keyCommand(tab,command,requestId);
  if([method isEqual:@"wheel"])return wheelCommand(tab,command,requestId);
  if([method isEqual:@"pinch"])return pinchCommand(tab,command,requestId);
  if([method hasPrefix:@"ime_"])return imeCommand(tab,method,command,requestId);
  if([method isEqual:@"frame_rate"]){
    // The window moved to a display with another refresh rate. CEF re-applies the
    // compositor v-sync interval and the capture rate live (UpdateFrameRate).
    if(!integer(command[@"frame_rate"],120)||([command[@"frame_rate"] intValue]!=60&&[command[@"frame_rate"] intValue]!=120)){failProtocol();return true;}
    event(@"accepted",@{@"request_id":requestId});
    auto* host=tab->browser->get_host(tab->browser);host->set_windowless_frame_rate(host,[command[@"frame_rate"] intValue]);host->base.release(&host->base);
    complete(requestId,@"success");return true;
  }
  return false;
}

static Ref<cef_keyboard_handler_t> keyboard;
static void configureInput(){
  client.api.get_keyboard_handler=[](cef_client_t*){return keyboard.retain();};
  keyboard.api.on_key_event=[](cef_keyboard_handler_t*,cef_browser_t* value,const cef_key_event_t* key,cef_event_handle_t)->int{
    HOLD(value);if(Tab* tab=tabFor(value))keyReport(tab,key);
    return 1;  // Zen owns every shortcut; the host has no menu of its own
  };
  render.api.on_ime_composition_range_changed=[](cef_render_handler_t*,cef_browser_t* value,const cef_range_t*,size_t count,cef_rect_t const* bounds){
    HOLD(value);Tab* tab=tabFor(value);if(!tab||!tab->textMode||!count||!bounds)return;
    count=std::min<size_t>(count,1024);
    double left=bounds[0].x,top=bounds[0].y,right=left+bounds[0].width,bottom=top+bounds[0].height;
    for(size_t i=1;i<count;i++){top=std::min(top,double(bounds[i].y));right=std::max(right,double(bounds[i].x+bounds[i].width));bottom=std::max(bottom,double(bounds[i].y+bounds[i].height));}
    setTextInput(tab,tab->textMode,left,top,std::max(1.0,right-left),std::max(1.0,bottom-top));
  };
  client.api.on_process_message_received=[](cef_client_t*,cef_browser_t* value,cef_frame_t* frame,cef_process_id_t source,cef_process_message_t* message)->int{
    HOLD(value);HOLD(frame);HOLD(message);
    if(source!=PID_RENDERER||!message)return 0;
    @autoreleasepool{
      if(![requestString(message->get_name(message)) isEqual:@"axio.text_input"])return 0;
      Tab* tab=tabFor(value);if(!tab||tab->closing)return 1;
      cef_list_value_t* args=message->get_argument_list(message);if(!args)return 1;
      bool valid=args->get_size(args)==5;int v[5]={0,0,0,0,0};
      for(size_t i=0;valid&&i<5;i++){valid=args->get_type(args,i)==VTYPE_INT;if(valid)v[i]=args->get_int(args,i);}
      args->base.release(&args->base);
      if(!valid||v[0]<0||v[0]>2)return 1;
      // Element bounds arrive in widget (physical) pixels; convert to logical points.
      double s=tab->scale>0?tab->scale:1,x=v[1]/s,y=v[2]/s,w=std::max(0,v[3])/s,h=std::max(0,v[4])/s;
      // Until composition reports real character bounds, place a one-line caret
      // at the start of the field (vertically centred in a single-line control).
      double line=std::clamp(h,1.0,32.0),top=h<=64?y+(h-line)/2:y+4;
      setTextInput(tab,v[0],x+4,top,1,line);
    }
    return 1;
  };
}

#ifdef AXIO_CEF_HELPER
// Renderer side of the text-input report. Runs in the sandboxed renderer helper and
// sends only the focused node's kind and bounds; no page text leaves the renderer.
static Ref<cef_app_t> helperApp;
static Ref<cef_render_process_handler_t> rendererHandler;
static decltype(&cef_process_message_create) p_process_message_create=nullptr;
static cef_app_t* rendererApp(){
  symbol(p_process_message_create,"cef_process_message_create");
  helperApp.api.get_render_process_handler=[](cef_app_t*){return rendererHandler.retain();};
  rendererHandler.api.on_focused_node_changed=[](cef_render_process_handler_t*,cef_browser_t* browser,cef_frame_t* frame,cef_domnode_t* node){
    HOLD(browser);HOLD(frame);HOLD(node);
    if(!frame)return;
    @autoreleasepool{
      int mode=0;cef_rect_t bounds{};
      if(node&&node->is_element(node)&&node->is_editable(node)){
        mode=node->is_form_control_element(node)&&node->get_form_control_element_type(node)==DOM_FORM_CONTROL_TYPE_INPUT_PASSWORD?2:1;
        bounds=node->get_element_bounds(node);
      }
      auto name=utf16(@"axio.text_input");auto text=view(name);
      cef_process_message_t* message=p_process_message_create(&text);if(!message)return;
      cef_list_value_t* args=message->get_argument_list(message);
      int values[5]={mode,bounds.x,bounds.y,bounds.width,bounds.height};
      for(size_t i=0;i<5;i++)args->set_int(args,i,values[i]);
      args->base.release(&args->base);
      frame->send_process_message(frame,PID_BROWSER,message);  // transfers our reference
    }
  };
  return helperApp.retain();
}
#endif
