// Mach IOSurface frame transport, host side. See contracts/engine-surface-v1.md.
//
// CEF 156 delivers PET_VIEW/PET_POPUP frames in IOSurfaces owned by viz's
// FrameSinkVideoCapturer pool (11 buffers). The buffer returns to that pool when
// OnAcceleratedPaint returns, so it is never forwarded: each frame is copied on
// the GPU into a host-owned ring of IOSurfaces per target, and only ring
// surfaces cross to Zen. The copy is awaited before returning to CEF because
// viz may overwrite the pooled buffer from another process immediately after.
//
// Ring surfaces are global (AXIO_SURFACE_FLAG_GLOBAL_SURFACE) so Gecko's GPU
// process, which resolves IOSurfaces by ID, can composite them without a second
// copy. A slot is reused only after Zen's RELEASE *and* once IOSurfaceIsInUse()
// is false (compositor, CALayer or WindowServer still reading it).
#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <IOSurface/IOSurface.h>
#import <Metal/Metal.h>
#include <bsm/libbsm.h>
#include <dispatch/dispatch.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <servers/bootstrap.h>
#include <unistd.h>
#include <algorithm>
#include <array>
#include <cstring>
#include "surface_transport.hpp"

namespace axio::surface {
namespace {

mach_port_t zenPort = MACH_PORT_NULL;    // send right to Zen's checked-in service
mach_port_t replyPort = MACH_PORT_NULL;  // our receive right; Zen holds its only send right
dispatch_source_t source = nil;
Callbacks callbacks;
bool isConnected = false, hasFailed = false;
id<MTLDevice> device = nil;
id<MTLCommandQueue> queue = nil;
uint64_t copyNanoseconds = 0;
mach_timebase_info_data_t timebase{};

uint64_t nanoseconds(uint64_t ticks) { return ticks * timebase.numer / timebase.denom; }

// Receive buffer large enough for every Zen->host message plus an audit trailer.
struct Received {
  union {
    mach_msg_header_t header;
    axio_surface_connected_msg_t connected;
    axio_surface_release_msg_t release;
    axio_surface_begin_frame_msg_t beginFrame;
    uint8_t bytes[256];
  };
  mach_msg_audit_trailer_t trailer;
};

void fail(const char* reason) {
  if (hasFailed) return;
  hasFailed = true;
  if (source) dispatch_source_cancel(source);
  if (callbacks.failed) callbacks.failed(reason);
}

// Returns MACH_MSG_SUCCESS, MACH_RCV_TIMED_OUT or another (fatal) error.
mach_msg_return_t receive(Received& buffer, mach_msg_timeout_t timeout) {
  memset(&buffer, 0, sizeof(buffer));
  return mach_msg(&buffer.header,
                  MACH_RCV_MSG | MACH_RCV_TIMEOUT |
                      MACH_RCV_TRAILER_TYPE(MACH_MSG_TRAILER_FORMAT_0) |
                      MACH_RCV_TRAILER_ELEMENTS(MACH_RCV_TRAILER_AUDIT),
                  0, sizeof(buffer), replyPort, timeout, MACH_PORT_NULL);
}

// Only the process that spawned this host may speak on the reply port. The send
// right was given only to Zen's service; the audit token is defence in depth.
bool fromParent(const Received& buffer) {
  const auto* trailer = reinterpret_cast<const mach_msg_audit_trailer_t*>(
      reinterpret_cast<const uint8_t*>(&buffer.header) + round_msg(buffer.header.msgh_size));
  if (reinterpret_cast<const uint8_t*>(trailer) + sizeof(*trailer) >
      reinterpret_cast<const uint8_t*>(&buffer) + sizeof(buffer)) return false;
  if (trailer->msgh_trailer_size < sizeof(mach_msg_audit_trailer_t)) return false;
  return audit_token_to_pid(trailer->msgh_audit) == getppid();
}

template <class T> bool simple(const Received& buffer) {
  const auto& h = buffer.header;
  if ((h.msgh_bits & MACH_MSGH_BITS_COMPLEX) || h.msgh_size != sizeof(T)) return false;
  const auto* value = reinterpret_cast<const T*>(&buffer.header);
  return value->magic == AXIO_SURFACE_MAGIC && value->version == AXIO_SURFACE_VERSION;
}

void drain() {
  for (int count = 0; count < 256 && !hasFailed; ++count) {
    Received buffer;
    mach_msg_return_t result = receive(buffer, 0);
    if (result == MACH_RCV_TIMED_OUT) return;
    if (result != MACH_MSG_SUCCESS) { fail("surface_receive_failed"); return; }
    if (buffer.header.msgh_bits & MACH_MSGH_BITS_COMPLEX) {
      mach_msg_destroy(&buffer.header);  // Never accept rights from Zen after connect.
      fail("surface_unexpected_rights");
      return;
    }
    // A reply right Zen attaches (e.g. send-once) is not used; drop it.
    if (buffer.header.msgh_remote_port != MACH_PORT_NULL) {
      mach_msg_destroy(&buffer.header);
      fail("surface_unexpected_reply_right");
      return;
    }
    if (!fromParent(buffer)) { fail("surface_wrong_sender"); return; }
    if (buffer.header.msgh_id == AXIO_SURFACE_MSG_RELEASE && simple<axio_surface_release_msg_t>(buffer)) {
      const auto& value = buffer.release;
      if (!value.frame_id || !callbacks.release || !callbacks.release(value.native_target_id, value.frame_id)) {
        fail("surface_invalid_release");
        return;
      }
    } else if (buffer.header.msgh_id == AXIO_SURFACE_MSG_BEGIN_FRAME &&
               simple<axio_surface_begin_frame_msg_t>(buffer)) {
      const auto& value = buffer.beginFrame;
      if (!value.sequence) { fail("surface_invalid_begin_frame"); return; }
      if (callbacks.beginFrame) callbacks.beginFrame(value.native_target_id, value.sequence);
    } else {
      fail("surface_unknown_message");
      return;
    }
  }
}

bool ensureMetal() {
  if (device) return true;
  device = MTLCreateSystemDefaultDevice();
  queue = [device newCommandQueue];
  return device && queue;
}

// kIOSurfaceIsGlobal is deprecated but still honoured; Gecko's MacIOSurface
// (gfx/2d/MacIOSurface.cpp) sets the same key for all of its surfaces.
CFStringRef globalKey() {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
  return kIOSurfaceIsGlobal;
#pragma clang diagnostic pop
}

IOSurfaceRef createSurface(int width, int height) {
  size_t row = IOSurfaceAlignProperty(kIOSurfaceBytesPerRow, size_t(width) * 4);
  NSDictionary* properties = @{
    (id)kIOSurfaceWidth: @(width), (id)kIOSurfaceHeight: @(height),
    (id)kIOSurfaceBytesPerElement: @4, (id)kIOSurfaceBytesPerRow: @(row),
    (id)kIOSurfaceAllocSize: @(IOSurfaceAlignProperty(kIOSurfaceAllocSize, row * size_t(height))),
    (id)kIOSurfacePixelFormat: @((uint32_t)'BGRA'),
    // Gecko's GPU process looks surfaces up by ID. Any same-user process that
    // learns the ID can too; see contracts/engine-surface-v1.md "Security".
    (__bridge id)globalKey(): @YES,
  };
  IOSurfaceRef surface = IOSurfaceCreate((__bridge CFDictionaryRef)properties);
  if (!surface) return nullptr;
  // Chromium's capture output is sRGB-encoded premultiplied BGRA.
  CGColorSpaceRef space = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
  if (CFPropertyListRef list = CGColorSpaceCopyPropertyList(space)) {
    IOSurfaceSetValue(surface, kIOSurfaceColorSpace, list);
    CFRelease(list);
  }
  CGColorSpaceRelease(space);
  return surface;
}

id<MTLTexture> wrap(IOSurfaceRef surface, int width, int height) {
  MTLTextureDescriptor* descriptor =
      [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                                         width:width height:height mipmapped:NO];
  descriptor.usage = MTLTextureUsageShaderRead;
  descriptor.storageMode = MTLStorageModeShared;
  return [device newTextureWithDescriptor:descriptor iosurface:surface plane:0];
}

id<MTLTexture> privateTexture(int width, int height) {
  MTLTextureDescriptor* descriptor =
      [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                                         width:width height:height mipmapped:NO];
  descriptor.usage = MTLTextureUsageShaderRead;
  descriptor.storageMode = MTLStorageModePrivate;
  return [device newTextureWithDescriptor:descriptor];
}

// Validates CEF's pooled surface and wraps it for this callback only.
id<MTLTexture> pooled(void* value, Rect visible) {
  auto surface = static_cast<IOSurfaceRef>(value);
  if (!surface || visible.x < 0 || visible.y < 0 || visible.width <= 0 || visible.height <= 0) return nil;
  if (IOSurfaceGetPixelFormat(surface) != 'BGRA') return nil;
  int width = int(IOSurfaceGetWidth(surface)), height = int(IOSurfaceGetHeight(surface));
  if (visible.x + visible.width > width || visible.y + visible.height > height) return nil;
  return wrap(surface, width, height);
}

void copy(id<MTLBlitCommandEncoder> blit, id<MTLTexture> from, int fx, int fy, int w, int h,
          id<MTLTexture> to, int tx, int ty) {
  [blit copyFromTexture:from sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(fx, fy, 0)
             sourceSize:MTLSizeMake(w, h, 1) toTexture:to destinationSlice:0 destinationLevel:0
      destinationOrigin:MTLOriginMake(tx, ty, 0)];
}

}  // namespace

struct Slot {
  IOSurfaceRef surface = nullptr;
  id<MTLTexture> texture = nil;
  int width = 0, height = 0;
  bool inFlight = false;
  uint64_t frame = 0;
};

class Target {
 public:
  std::array<Slot, AXIO_SURFACE_MAX_IN_FLIGHT> slots;
  id<MTLTexture> view = nil, popup = nil;  // Private GPU copies, only while a popup is shown.
  int popupX = 0, popupY = 0;
  bool popupShown = false;
  ~Target() {
    for (auto& slot : slots) if (slot.surface) CFRelease(slot.surface);
  }
  Slot* acquire(int width, int height) {
    Slot* chosen = nullptr;
    for (auto& slot : slots) {
      if (slot.inFlight) continue;
      // Zen acknowledged it; a surface still in use by WindowServer is skipped anyway.
      if (slot.surface && IOSurfaceIsInUse(slot.surface)) continue;
      if (slot.width == width && slot.height == height) return &slot;
      if (!chosen) chosen = &slot;
    }
    if (!chosen) return nullptr;
    if (chosen->surface) CFRelease(chosen->surface);
    chosen->surface = createSurface(width, height);
    chosen->texture = chosen->surface ? wrap(chosen->surface, width, height) : nil;
    chosen->width = chosen->texture ? width : 0;
    chosen->height = chosen->texture ? height : 0;
    return chosen->texture ? chosen : nullptr;
  }
};

namespace {

Result send(Target* target, Slot* slot, FrameMeta meta, uint32_t flags, uint64_t started) {
  mach_port_t port = IOSurfaceCreateMachPort(slot->surface);
  if (port == MACH_PORT_NULL) return Result::Failed;
  axio_surface_frame_msg_t message{};
  message.header.msgh_bits = MACH_MSGH_BITS_SET(MACH_MSG_TYPE_COPY_SEND, 0, 0, MACH_MSGH_BITS_COMPLEX);
  message.header.msgh_size = sizeof(message);
  message.header.msgh_remote_port = zenPort;
  message.header.msgh_id = AXIO_SURFACE_MSG_FRAME;
  message.body.msgh_descriptor_count = 1;
  message.surface.name = port;
  message.surface.disposition = MACH_MSG_TYPE_MOVE_SEND;
  message.surface.type = MACH_MSG_PORT_DESCRIPTOR;
  auto& data = message.data;
  data.magic = AXIO_SURFACE_MAGIC;
  data.version = AXIO_SURFACE_VERSION;
  data.native_target_id = meta.target;
  data.document_generation = meta.documentGeneration;
  data.navigation_generation = meta.navigationGeneration;
  data.frame_id = meta.frameId;
  data.paint_time = meta.paintTime;
  data.begin_frame_sequence = meta.beginFrameSequence;
  data.width = uint32_t(slot->width);
  data.height = uint32_t(slot->height);
  data.logical_width = meta.logicalWidth;
  data.logical_height = meta.logicalHeight;
  data.device_scale = meta.scale;
  data.format = AXIO_SURFACE_FORMAT_BGRA8_PREMULTIPLIED_SRGB;
  data.surface_id = IOSurfaceGetID(slot->surface);
  data.flags = flags | AXIO_SURFACE_FLAG_GLOBAL_SURFACE;
  if (meta.dirty && meta.dirtyCount <= AXIO_SURFACE_MAX_DIRTY) {
    data.dirty_count = uint32_t(meta.dirtyCount);
    for (size_t i = 0; i < meta.dirtyCount; ++i)
      data.dirty[i] = {meta.dirty[i].x, meta.dirty[i].y, meta.dirty[i].width, meta.dirty[i].height};
  }
  // Record credit before sending: a release may be processed right after.
  slot->inFlight = true;
  slot->frame = meta.frameId;
  copyNanoseconds = nanoseconds(mach_absolute_time() - started);
  data.send_time = mach_absolute_time();
  // Zen's queue limit covers every frame the host may have in flight, so a full
  // queue is a protocol violation: never block the CEF UI thread on Zen.
  mach_msg_return_t result = mach_msg(&message.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT, sizeof(message), 0,
                                      MACH_PORT_NULL, 0, MACH_PORT_NULL);
  if (result != MACH_MSG_SUCCESS) {
    slot->inFlight = false;
    mach_port_deallocate(mach_task_self(), message.surface.name);
    fail(result == MACH_SEND_TIMED_OUT ? "surface_queue_full" : "surface_send_failed");
    return Result::Failed;
  }
  (void)target;
  return Result::Sent;
}

bool commit(id<MTLCommandBuffer> buffer) {
  [buffer commit];
  [buffer waitUntilCompleted];
  return buffer.status == MTLCommandBufferStatusCompleted;
}

}  // namespace

bool connect(const std::string& service, const uint8_t (&token)[AXIO_SURFACE_TOKEN_BYTES], int timeoutMs,
             std::string& error) {
  mach_timebase_info(&timebase);
  if (!ensureMetal()) { error = "metal_unavailable"; return false; }
  // bootstrap_look_up is the supported, non-deprecated Mach lookup used by
  // Chromium's own rendezvous; the name arrived only through the private pipe.
  if (bootstrap_look_up(bootstrap_port, service.c_str(), &zenPort) != KERN_SUCCESS) {
    zenPort = MACH_PORT_NULL; error = "surface_service_not_found"; return false;
  }
  if (mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &replyPort) != KERN_SUCCESS) {
    error = "surface_port_allocation_failed"; return false;
  }
  mach_port_limits_t limits{MACH_PORT_QLIMIT_LARGE};
  mach_port_set_attributes(mach_task_self(), replyPort, MACH_PORT_LIMITS_INFO,
                           reinterpret_cast<mach_port_info_t>(&limits), MACH_PORT_LIMITS_INFO_COUNT);
  axio_surface_connect_msg_t message{};
  message.header.msgh_bits = MACH_MSGH_BITS_SET(MACH_MSG_TYPE_COPY_SEND, 0, 0, MACH_MSGH_BITS_COMPLEX);
  message.header.msgh_size = sizeof(message);
  message.header.msgh_remote_port = zenPort;
  message.header.msgh_id = AXIO_SURFACE_MSG_CONNECT;
  message.body.msgh_descriptor_count = 1;
  message.reply_port.name = replyPort;
  message.reply_port.disposition = MACH_MSG_TYPE_MAKE_SEND;
  message.reply_port.type = MACH_MSG_PORT_DESCRIPTOR;
  message.data.magic = AXIO_SURFACE_MAGIC;
  message.data.version = AXIO_SURFACE_VERSION;
  memcpy(message.data.token, token, AXIO_SURFACE_TOKEN_BYTES);
  message.data.host_pid = uint32_t(getpid());
  mach_msg_return_t sent = mach_msg(&message.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT, sizeof(message), 0,
                                    MACH_PORT_NULL, mach_msg_timeout_t(timeoutMs), MACH_PORT_NULL);
  memset(&message.data.token, 0, sizeof(message.data.token));
  if (sent != MACH_MSG_SUCCESS) { error = "surface_connect_send_failed"; return false; }
  Received buffer;
  mach_msg_return_t received = receive(buffer, mach_msg_timeout_t(timeoutMs));
  if (received != MACH_MSG_SUCCESS) { error = "surface_connect_timeout"; return false; }
  if ((buffer.header.msgh_bits & MACH_MSGH_BITS_COMPLEX) || buffer.header.msgh_remote_port != MACH_PORT_NULL) {
    mach_msg_destroy(&buffer.header); error = "surface_connect_rights"; return false;
  }
  if (buffer.header.msgh_id != AXIO_SURFACE_MSG_CONNECTED || !simple<axio_surface_connected_msg_t>(buffer)) {
    error = "surface_connect_invalid"; return false;
  }
  if (!fromParent(buffer)) { error = "surface_connect_wrong_sender"; return false; }
  isConnected = true;
  return true;
}

void start(Callbacks value) {
  callbacks = std::move(value);
  source = dispatch_source_create(DISPATCH_SOURCE_TYPE_MACH_RECV, replyPort, 0, dispatch_get_main_queue());
  dispatch_source_set_event_handler(source, ^{ @autoreleasepool { drain(); } });
  dispatch_resume(source);
}

bool connected() { return isConnected && !hasFailed; }

void shutdown() {
  if (source) { dispatch_source_cancel(source); source = nil; }
  if (replyPort != MACH_PORT_NULL) {
    mach_port_mod_refs(mach_task_self(), replyPort, MACH_PORT_RIGHT_RECEIVE, -1);
    replyPort = MACH_PORT_NULL;
  }
  if (zenPort != MACH_PORT_NULL) { mach_port_deallocate(mach_task_self(), zenPort); zenPort = MACH_PORT_NULL; }
  isConnected = false;
}

Target* createTarget() { return new Target(); }
void destroyTarget(Target* target) { delete target; }

Result paintView(Target* target, void* cefSurface, Rect visible, int width, int height, FrameMeta meta) {
  uint64_t started = mach_absolute_time();
  if (!target || !connected() || visible.width != width || visible.height != height) return Result::Invalid;
  @autoreleasepool {
    id<MTLTexture> from = pooled(cefSurface, visible);
    if (!from) return Result::Invalid;
    Slot* slot = target->acquire(width, height);
    if (!slot) return Result::NoFreeSlot;
    bool composite = target->popupShown && target->popup;
    if (target->popupShown && (!target->view || int(target->view.width) != width || int(target->view.height) != height))
      target->view = privateTexture(width, height);
    id<MTLCommandBuffer> buffer = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [buffer blitCommandEncoder];
    copy(blit, from, visible.x, visible.y, width, height, slot->texture, 0, 0);
    if (target->popupShown && target->view) copy(blit, from, visible.x, visible.y, width, height, target->view, 0, 0);
    [blit endEncoding];
    if (composite) {
      // A separate pass orders the popup after the view copy into the same slot.
      id<MTLBlitCommandEncoder> overlay = [buffer blitCommandEncoder];
      int x0 = std::max(0, target->popupX), y0 = std::max(0, target->popupY);
      int x1 = std::min(width, target->popupX + int(target->popup.width));
      int y1 = std::min(height, target->popupY + int(target->popup.height));
      if (x1 > x0 && y1 > y0)
        copy(overlay, target->popup, x0 - target->popupX, y0 - target->popupY, x1 - x0, y1 - y0, slot->texture, x0, y0);
      [overlay endEncoding];
    }
    if (!commit(buffer)) { fail("surface_gpu_copy_failed"); return Result::Failed; }
    return send(target, slot, meta, composite ? AXIO_SURFACE_FLAG_POPUP_COMPOSITED : 0, started);
  }
}

Result paintPopup(Target* target, void* cefSurface, Rect visible, int originX, int originY, FrameMeta meta) {
  uint64_t started = mach_absolute_time();
  if (!target || !connected() || !target->popupShown) return Result::Invalid;
  @autoreleasepool {
    id<MTLTexture> from = pooled(cefSurface, visible);
    if (!from || visible.width > 4096 || visible.height > 4096) return Result::Invalid;
    if (!target->popup || int(target->popup.width) != visible.width || int(target->popup.height) != visible.height)
      target->popup = privateTexture(visible.width, visible.height);
    target->popupX = originX;
    target->popupY = originY;
    id<MTLTexture> view = target->view;
    Slot* slot = view ? target->acquire(int(view.width), int(view.height)) : nullptr;
    id<MTLCommandBuffer> buffer = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [buffer blitCommandEncoder];
    copy(blit, from, visible.x, visible.y, visible.width, visible.height, target->popup, 0, 0);
    if (slot) copy(blit, view, 0, 0, int(view.width), int(view.height), slot->texture, 0, 0);
    [blit endEncoding];
    if (slot) {
      id<MTLBlitCommandEncoder> overlay = [buffer blitCommandEncoder];
      int width = int(view.width), height = int(view.height);
      int x0 = std::max(0, originX), y0 = std::max(0, originY);
      int x1 = std::min(width, originX + visible.width), y1 = std::min(height, originY + visible.height);
      if (x1 > x0 && y1 > y0) copy(overlay, target->popup, x0 - originX, y0 - originY, x1 - x0, y1 - y0, slot->texture, x0, y0);
      [overlay endEncoding];
    }
    if (!commit(buffer)) { fail("surface_gpu_copy_failed"); return Result::Failed; }
    if (!view) return Result::Cached;  // Composited on the next view paint.
    if (!slot) return Result::NoFreeSlot;
    meta.dirty = nullptr; meta.dirtyCount = 0;  // Popup movement dirties the whole composite.
    return send(target, slot, meta, AXIO_SURFACE_FLAG_POPUP_COMPOSITED, started);
  }
}

void setPopupVisible(Target* target, bool visible) {
  if (!target) return;
  target->popupShown = visible;
  target->popup = nil;
  target->view = nil;
}

bool popupVisible(Target* target) { return target && target->popupShown; }

bool release(Target* target, uint64_t frameId) {
  if (!target) return false;
  for (auto& slot : target->slots)
    if (slot.inFlight && slot.frame == frameId) { slot.inFlight = false; return true; }
  return false;
}

size_t inFlight(Target* target) {
  size_t count = 0;
  if (target) for (auto& slot : target->slots) count += slot.inFlight;
  return count;
}

uint64_t lastCopyNanoseconds() { return copyNanoseconds; }

}  // namespace axio::surface
