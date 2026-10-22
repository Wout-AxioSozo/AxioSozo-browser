// AxioSozo Chromium accessibility mirror. Pure C++17: no CEF, AppKit or
// Objective-C, so tests/a11y_core_test.cc compiles it alone.
//
// Input: CEF's OnAccessibilityTreeChange / OnAccessibilityLocationChange payloads,
// in the shape produced by libcef/browser/osr/osr_accessibility_util.cc (CEF
// 062ebe433bf6 / 154.0.23, unchanged in the pinned 47dbc1df023e / 156.0.7
// runtime), converted from cef_value_t into
// the bounded Value below by a11y.inc.
//
// Output: complete cef-v1 JSON event lines `ax_tree_update` and `ax_location`
// (proposed in docs/design/engine-accessibility.md §6), each at most
// MaxEventBytes, so they fit transport.hpp's MaxMeta.
//
// What this layer guarantees, whatever the page or renderer sends:
//   * one flat id space per target: (ax_tree_id, node id) -> wire id, never reused
//     within a target, so Zen never sees Chromium tree UUIDs;
//   * password fields (state `protected` or inputType `password`) never carry a
//     value, selection or descendants;
//   * bounded memory (MaxNodes, MaxTrees, MaxKids) and bounded events (byte
//     budgets per string, continuation records instead of oversized events);
//   * flow control: at most MaxOutstanding unacknowledged events per target, so
//     the transport's 512-event queue can never overflow because of accessibility.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <limits>
#include <memory>
#include <set>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace axio::ax {

constexpr size_t MaxEventBytes = 8192;   // transport.hpp MaxMeta
constexpr size_t EventSlack = 128;       // never fill an event to the last byte
constexpr size_t MaxNodes = 25000;       // per target; inline text boxes are not kept
constexpr size_t MaxTrees = 64;          // main frame plus out-of-process iframes
constexpr size_t MaxKids = 20000;        // children kept per node
constexpr size_t MaxEventsPerBatch = 32; // forwarded renderer events per batch
constexpr size_t MaxOutstanding = 4;     // unacknowledged ax events per target
constexpr size_t MaxRetired = 256;       // remembered replaced tree ids
constexpr double RetiredMs = 2000;       // late updates of a replaced document are ignored this long
constexpr double BatchIntervalMs = 100;  // coalescing window between batch starts
// Escaped-byte budgets. `name`/`value` continue in `append` records up to the totals.
constexpr size_t NamePiece = 1024, NameTotal = 16384;
constexpr size_t ValuePiece = 1024, ValueTotal = 8192;
constexpr size_t DescBytes = 512, PlaceholderBytes = 256, UrlBytes = 1024, ShortBytes = 128, TokenBytes = 32;

// ---- Value: the bounded JSON-like shape of a cef_value_t ------------------------
struct Value {
  enum class Type : uint8_t { Null, Bool, Number, String, List, Dict };
  Type type = Type::Null;
  bool boolean = false;
  double number = 0;
  std::string text;  // UTF-8
  std::vector<Value> items;
  std::vector<std::pair<std::string, Value>> fields;

  bool isString() const { return type == Type::String; }
  bool isList() const { return type == Type::List; }
  bool isDict() const { return type == Type::Dict; }
  const Value* get(const char* key) const {
    if (type != Type::Dict) return nullptr;
    for (const auto& field : fields)
      if (field.first == key) return &field.second;
    return nullptr;
  }
  // Chromium ids arrive as int; cefclient's CastToInt also accepts decimal strings.
  bool toInt(long long& out) const {
    if (type == Type::Number && std::isfinite(number) && std::floor(number) == number &&
        std::fabs(number) <= 2147483647.0) {
      out = static_cast<long long>(number);
      return true;
    }
    if (type == Type::String && !text.empty() && text.size() <= 11) {
      char* end = nullptr;
      long long parsed = std::strtoll(text.c_str(), &end, 10);
      if (end && *end == 0 && parsed >= INT32_MIN && parsed <= INT32_MAX) {
        out = parsed;
        return true;
      }
    }
    return false;
  }
  bool toDouble(double& out) const {
    if (type != Type::Number || !std::isfinite(number)) return false;
    out = number;
    return true;
  }
};

inline bool intField(const Value& v, const char* key, long long& out) {
  const Value* field = v.get(key);
  return field && field->toInt(out);
}
inline const std::string* stringField(const Value& v, const char* key) {
  const Value* field = v.get(key);
  return field && field->isString() ? &field->text : nullptr;
}

// ---- UTF-8 and JSON text ------------------------------------------------------------
// Decodes one code point; an invalid sequence yields U+FFFD and consumes one byte.
inline uint32_t decodeUtf8(const std::string& s, size_t& i) {
  unsigned char c = static_cast<unsigned char>(s[i]);
  if (c < 0x80) { i++; return c; }
  int extra = (c >= 0xC2 && c <= 0xDF) ? 1 : (c >= 0xE0 && c <= 0xEF) ? 2 : (c >= 0xF0 && c <= 0xF4) ? 3 : -1;
  if (extra < 0 || i + size_t(extra) >= s.size()) { i++; return 0xFFFD; }
  uint32_t cp = c & (extra == 1 ? 0x1F : extra == 2 ? 0x0F : 0x07);
  for (int k = 1; k <= extra; k++) {
    unsigned char d = static_cast<unsigned char>(s[i + k]);
    if ((d & 0xC0) != 0x80) { i++; return 0xFFFD; }
    cp = (cp << 6) | (d & 0x3F);
  }
  if ((extra == 2 && cp < 0x800) || (extra == 3 && (cp < 0x10000 || cp > 0x10FFFF)) || (cp >= 0xD800 && cp <= 0xDFFF)) {
    i++;
    return 0xFFFD;
  }
  i += extra + 1;
  return cp;
}
inline void appendUtf8(std::string& out, uint32_t cp) {
  if (cp < 0x80) out += char(cp);
  else if (cp < 0x800) { out += char(0xC0 | (cp >> 6)); out += char(0x80 | (cp & 0x3F)); }
  else if (cp < 0x10000) { out += char(0xE0 | (cp >> 12)); out += char(0x80 | ((cp >> 6) & 0x3F)); out += char(0x80 | (cp & 0x3F)); }
  else { out += char(0xF0 | (cp >> 18)); out += char(0x80 | ((cp >> 12) & 0x3F)); out += char(0x80 | ((cp >> 6) & 0x3F)); out += char(0x80 | (cp & 0x3F)); }
}
// Bytes one code point takes inside a JSON string literal written by appendJson.
inline size_t escapedBytes(uint32_t cp) {
  if (cp == '"' || cp == '\\' || cp == '\n' || cp == '\r' || cp == '\t' || cp == '\b' || cp == '\f') return 2;
  if (cp < 0x20 || cp == 0x2028 || cp == 0x2029) return 6;
  return cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
}
inline size_t escapedSize(const std::string& s) {
  size_t total = 0;
  for (size_t i = 0; i < s.size();) total += escapedBytes(decodeUtf8(s, i));
  return total;
}
inline void appendJson(std::string& out, const std::string& s) {
  out += '"';
  for (size_t i = 0; i < s.size();) {
    uint32_t cp = decodeUtf8(s, i);
    switch (cp) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      default:
        if (cp < 0x20 || cp == 0x2028 || cp == 0x2029) {
          char buffer[8];
          std::snprintf(buffer, sizeof buffer, "\\u%04x", cp);
          out += buffer;
        } else appendUtf8(out, cp);
    }
  }
  out += '"';
}
// Sanitized text whose escaped form fits `budget` bytes. Clipped text ends with
// U+2026 and never splits a code point.
inline std::string clipText(const std::string& s, size_t budget) {
  std::string out;
  if (escapedSize(s) <= budget) {
    for (size_t i = 0; i < s.size();) appendUtf8(out, decodeUtf8(s, i));
    return out;
  }
  if (budget < 3) return out;
  size_t used = 0;
  for (size_t i = 0; i < s.size();) {
    uint32_t cp = decodeUtf8(s, i);
    size_t bytes = escapedBytes(cp);
    if (used + bytes > budget - 3) break;
    appendUtf8(out, cp);
    used += bytes;
  }
  appendUtf8(out, 0x2026);
  return out;
}
// Splits already-clipped text into pieces of at most `first` then `rest` escaped bytes.
inline std::vector<std::string> splitText(const std::string& s, size_t first, size_t rest) {
  std::vector<std::string> pieces(1);
  size_t used = 0, limit = first;
  for (size_t i = 0; i < s.size();) {
    size_t start = i;
    uint32_t cp = decodeUtf8(s, i);
    size_t bytes = escapedBytes(cp);
    if (used + bytes > limit) { pieces.emplace_back(); used = 0; limit = rest; }
    pieces.back().append(s, start, i - start);
    used += bytes;
  }
  return pieces;
}
// Short identifiers (roles, enum tokens) are a strict ASCII token or dropped.
inline bool token(const std::string& s, size_t maximum) {
  if (s.empty() || s.size() > maximum) return false;
  for (char c : s)
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-' || c == ' ')) return false;
  return true;
}
inline void appendNumber(std::string& out, double value) {
  if (!std::isfinite(value)) value = 0;
  value = std::clamp(value, -1e7, 1e7);
  double rounded = std::round(value * 100) / 100;
  if (rounded == 0) rounded = 0;  // no "-0"
  char buffer[40];
  if (rounded == std::floor(rounded)) std::snprintf(buffer, sizeof buffer, "%.0f", rounded);
  else {
    std::snprintf(buffer, sizeof buffer, "%.2f", rounded);
    size_t n = std::strlen(buffer);
    while (n > 0 && buffer[n - 1] == '0') buffer[--n] = 0;
  }
  out += buffer;
}

// ---- Geometry -----------------------------------------------------------------------
struct Rect { double x = 0, y = 0, w = 0, h = 0; };
// x' = a x + c y + e;  y' = b x + d y + f  (CSS matrix() order)
struct Affine { double a = 1, b = 0, c = 0, d = 1, e = 0, f = 0; };
inline Rect mapRect(const Affine& m, const Rect& r) {
  double xs[4] = {r.x, r.x + r.w, r.x, r.x + r.w}, ys[4] = {r.y, r.y, r.y + r.h, r.y + r.h};
  double minX = 1e300, minY = 1e300, maxX = -1e300, maxY = -1e300;
  for (int i = 0; i < 4; i++) {
    double x = m.a * xs[i] + m.c * ys[i] + m.e, y = m.b * xs[i] + m.d * ys[i] + m.f;
    minX = std::min(minX, x); maxX = std::max(maxX, x); minY = std::min(minY, y); maxY = std::max(maxY, y);
  }
  return {minX, minY, maxX - minX, maxY - minY};
}
// gfx::Transform::ToString() prints the 4x4 matrix row-major. The 2D affine part is
// kept (perspective and z are ignored). A 4-number "[sx sy], [tx ty]" axis form is
// accepted too. [unverified: exact format of the pinned Chromium's ToString()]
inline bool parseTransform(const std::string& s, Affine& out) {
  double v[16];
  int n = 0;
  const char* p = s.c_str();
  while (*p) {
    if ((*p >= '0' && *p <= '9') || *p == '-' || *p == '+' || *p == '.') {
      char* end = nullptr;
      double d = std::strtod(p, &end);
      if (end == p) { p++; continue; }
      if (n >= 16 || !std::isfinite(d) || std::fabs(d) > 1e7) return false;
      v[n++] = d;
      p = end;
    } else p++;
  }
  if (n == 16) out = {v[0], v[4], v[1], v[5], v[3], v[7]};
  else if (n == 4) out = {v[0], 0, 0, v[1], v[2], v[3]};
  else return false;
  return true;
}

// ---- Allowlists ----------------------------------------------------------------------
inline const char* const* stateNames(size_t& count) {
  static const char* const names[] = {"autofillAvailable", "collapsed", "default", "editable", "expanded", "focusable",
    "horizontal", "hovered", "ignored", "invisible", "linked", "multiline", "multiselectable", "protected",
    "required", "richlyEditable", "vertical", "visited"};
  count = sizeof names / sizeof names[0];
  return names;
}
inline int stateBit(const std::string& name) {
  size_t count = 0;
  const char* const* names = stateNames(count);
  for (size_t i = 0; i < count; i++)
    if (name == names[i]) return int(i);
  return -1;
}
enum : uint32_t { StateEditable = 1u << 3, StateFocusable = 1u << 5, StateIgnored = 1u << 8, StateInvisible = 1u << 9,
  StateProtected = 1u << 13, StateRichlyEditable = 1u << 15 };
inline bool actionAllowed(const std::string& name) {
  static const char* const names[] = {"doDefault", "focus", "blur", "increment", "decrement", "scrollToMakeVisible",
    "setValue", "showContextMenu", "expand", "collapse"};
  for (const char* item : names)
    if (name == item) return true;
  return false;
}
inline bool eventAllowed(const std::string& name) {
  static const char* const names[] = {"focus", "blur", "alert", "liveRegionChanged", "loadComplete", "valueChanged",
    "textSelectionChanged", "documentSelectionChanged", "menuStart", "menuEnd", "menuPopupStart", "menuPopupEnd",
    "expandedChanged", "checkedStateChanged", "selectedChildrenChanged", "scrolledToAnchor", "activeDescendantChanged"};
  for (const char* item : names)
    if (name == item) return true;
  return false;
}

// ---- Mirror node ---------------------------------------------------------------------
struct Node {
  int id = 0;
  uint32_t wire = 0;    // wire id sent to Zen
  int parent = 0;       // tree-local parent (0 = none); maintained by collect()
  bool hidden = false;  // inside a password field: never sent
  std::string role;
  uint32_t states = 0;
  std::vector<std::string> actions;
  std::vector<int> kids;
  int offsetContainer = 0;
  Rect local;
  bool hasTransform = false;
  Affine transform;
  bool hasScroll = false;
  double scrollX = 0, scrollY = 0;
  std::string childTree;
  std::string name, value, desc, placeholder, url, roledesc, shortcuts, lang;
  std::string checked, invalid, restriction, live, relevant, current, input, action, tag;
  long long level = -1, popup = -1, setsize = -1, posinset = -1, selStart = -1, selEnd = -1;
  long long table[6] = {-1, -1, -1, -1, -1, -1};  // rows, cols, row, col, rowspan, colspan
  long long activedesc = 0, linktarget = 0;          // tree-local ids
  bool hasRange = false;
  double range[4] = {NAN, NAN, NAN, NAN};             // min, max, value, step
  int8_t atomic = -1, busy = -1, selected = -1, modal = -1;
  bool isProtected() const { return (states & StateProtected) || input == "password"; }
};

struct Tree {
  std::string id, parentId, focusedTree;
  bool hasData = false;
  int root = 0, focus = 0;
  std::unordered_map<int, Node> nodes;
};

inline void parseNode(const Value& v, Node& n) {
  const std::string* role = stringField(v, "role");
  n.role = role && token(*role, 40) && role->find(' ') == std::string::npos ? *role : "unknown";
  n.states = 0;
  if (const Value* states = v.get("state"); states && states->isList())
    for (const auto& item : states->items)
      if (item.isString()) if (int bit = stateBit(item.text); bit >= 0) n.states |= 1u << bit;
  n.actions.clear();
  if (const Value* actions = v.get("actions"); actions && actions->isList())
    for (const auto& item : actions->items)
      if (item.isString() && actionAllowed(item.text) && n.actions.size() < 16) n.actions.push_back(item.text);
  n.kids.clear();
  if (const Value* kids = v.get("child_ids"); kids && kids->isList())
    for (const auto& item : kids->items) {
      long long id = 0;
      if (n.kids.size() >= MaxKids) break;
      if (item.toInt(id) && id > 0) n.kids.push_back(int(id));
    }
  long long container = 0;
  n.offsetContainer = intField(v, "offset_container_id", container) && container > 0 ? int(container) : 0;
  n.local = Rect{};
  if (const Value* location = v.get("location"); location && location->isDict()) {
    double x = 0, y = 0, w = 0, h = 0;
    const Value *vx = location->get("x"), *vy = location->get("y"), *vw = location->get("width"), *vh = location->get("height");
    if (vx) vx->toDouble(x);
    if (vy) vy->toDouble(y);
    if (vw) vw->toDouble(w);
    if (vh) vh->toDouble(h);
    n.local = {std::clamp(x, -1e7, 1e7), std::clamp(y, -1e7, 1e7), std::clamp(w, 0.0, 1e7), std::clamp(h, 0.0, 1e7)};
  }
  n.hasTransform = false;
  if (const std::string* transform = stringField(v, "transform"); transform && transform->size() <= 1024)
    n.hasTransform = parseTransform(*transform, n.transform);

  const Value empty;
  const Value* attrs = v.get("attributes");
  const Value& a = attrs && attrs->isDict() ? *attrs : empty;
  auto text = [&](const char* key, size_t budget) -> std::string {
    const std::string* s = stringField(a, key);
    return s ? clipText(*s, budget) : std::string();
  };
  auto tok = [&](const char* key) -> std::string {
    const std::string* s = stringField(a, key);
    return s && token(*s, TokenBytes) ? *s : std::string();
  };
  auto number = [&](const char* key) -> long long {
    long long out = -1;
    return intField(a, key, out) ? out : -1;
  };
  auto flag = [&](const char* key) -> int8_t {
    const Value* b = a.get(key);
    return b && b->type == Value::Type::Bool ? int8_t(b->boolean) : int8_t(-1);
  };
  n.name = text("name", NameTotal);
  n.value = text("value", ValueTotal);
  n.desc = text("description", DescBytes);
  n.placeholder = text("placeholder", PlaceholderBytes);
  n.roledesc = text("roleDescription", ShortBytes);
  n.shortcuts = text("keyShortcuts", ShortBytes);
  n.lang = tok("language");
  // URLs are sent whole or not at all, and only for web schemes.
  n.url.clear();
  if (const std::string* url = stringField(a, "url"); url && (url->rfind("https://", 0) == 0 || url->rfind("http://", 0) == 0) &&
      escapedSize(*url) <= UrlBytes && clipText(*url, UrlBytes) == *url)
    n.url = *url;
  n.checked = tok("checkedState");
  n.invalid = tok("invalidState");
  n.restriction = tok("restriction");
  n.live = tok("liveStatus");
  if (n.live.empty()) n.live = tok("containerLiveStatus");
  n.relevant = tok("liveRelevant");
  if (n.relevant.empty()) n.relevant = tok("containerLiveRelevant");
  n.current = tok("ariaCurrentState");
  n.input = tok("inputType");
  n.action = tok("defaultActionVerb");
  n.tag = tok("htmlTag");
  n.childTree.clear();
  if (const std::string* child = stringField(a, "childTreeId"); child && !child->empty() && child->size() <= 128) n.childTree = *child;
  n.level = number("hierarchicalLevel");
  n.popup = number("hasPopup");
  n.setsize = number("setSize");
  n.posinset = number("posInSet");
  n.selStart = number("textSelStart");
  n.selEnd = number("textSelEnd");
  n.table[0] = number("tableRowCount") >= 0 ? number("tableRowCount") : number("ariaRowCount");
  n.table[1] = number("tableColumnCount") >= 0 ? number("tableColumnCount") : number("ariaColumnCount");
  n.table[2] = number("tableCellRowIndex") >= 0 ? number("tableCellRowIndex") : number("tableRowIndex");
  n.table[3] = number("tableCellColumnIndex") >= 0 ? number("tableCellColumnIndex") : number("tableColumnIndex");
  n.table[4] = number("tableCellRowSpan");
  n.table[5] = number("tableCellColumnSpan");
  long long id = 0;
  n.activedesc = intField(a, "activedescendantId", id) && id > 0 ? id : 0;
  n.linktarget = intField(a, "inPageLinkTargetId", id) && id > 0 ? id : 0;
  const char* rangeKeys[4] = {"minValueForRange", "maxValueForRange", "valueForRange", "stepValueForRange"};
  n.hasRange = false;
  for (int i = 0; i < 4; i++) {
    n.range[i] = NAN;
    if (const Value* r = a.get(rangeKeys[i]); r && r->toDouble(n.range[i])) n.hasRange = true;
  }
  n.atomic = flag("liveAtomic");
  if (n.atomic < 0) n.atomic = flag("containerLiveAtomic");
  n.busy = flag("busy");
  n.selected = flag("selected");
  n.modal = flag("modal");
  double sx = 0, sy = 0;
  const Value *vsx = a.get("scrollX"), *vsy = a.get("scrollY");
  n.hasScroll = vsx && vsy && vsx->toDouble(sx) && vsy->toDouble(sy);
  n.scrollX = n.hasScroll ? sx : 0;
  n.scrollY = n.hasScroll ? sy : 0;
}

// ---- Mirror ------------------------------------------------------------------------
// One per target. UI-thread owned in the host; not thread-safe.
class Mirror {
 public:
  enum class Ack { Released, Stale, Unknown };

  /** New epoch: enable, or a new main-frame document. Zen replaces its model. */
  void reset() {
    clearAll();
    enabled_ = true;
    resetPending_ = true;
  }
  /** Accessibility off: nothing more is sent; late acks are ignored. */
  void disable() {
    clearAll();
    enabled_ = false;
    outstanding_.clear();
  }
  bool enabled() const { return enabled_; }
  bool truncated() const { return truncated_; }
  size_t nodeCount() const { return count_; }

  void treeChange(const Value& v, double nowMs) {
    if (!enabled_) return;
    now_ = nowMs;
    const std::string* treeId = stringField(v, "ax_tree_id");
    if (!treeId || treeId->empty() || treeId->size() > 128 || retiredRecently(*treeId)) return;
    if (const Value* updates = v.get("updates"); updates && updates->isList())
      for (const auto& update : updates->items)
        if (update.isDict()) applyUpdate(*treeId, update);
    if (const Value* events = v.get("events"); events && events->isList())
      for (const auto& e : events->items) {
        const std::string* type = stringField(e, "event_type");
        long long id = 0;
        if (!type || !eventAllowed(*type) || !intField(e, "id", id) || events_.size() >= MaxEventsPerBatch) continue;
        if (const Node* node = find(*treeId, int(id)); node && !node->hidden) events_.push_back({*type, node->wire});
      }
  }

  void locationChange(const Value& v) {
    if (!enabled_ || !v.isList()) return;
    for (const auto& change : v.items) {
      const std::string* treeId = stringField(change, "ax_tree_id");
      long long id = 0;
      const Value* location = change.get("new_location");
      if (!treeId || !intField(change, "id", id) || !location || !location->isDict()) continue;
      Node* node = findMutable(*treeId, int(id));
      if (!node) continue;
      Value shaped;  // reuse parseNode's geometry rules on {location, offset_container_id, transform}
      shaped.type = Value::Type::Dict;
      if (const Value* bounds = location->get("bounds")) shaped.fields.push_back({"location", *bounds});
      if (const Value* container = location->get("offset_container_id")) shaped.fields.push_back({"offset_container_id", *container});
      if (const Value* transform = location->get("transform")) shaped.fields.push_back({"transform", *transform});
      Node geometry;
      parseNode(shaped, geometry);
      node->local = geometry.local;
      node->offsetContainer = geometry.offsetContainer;
      node->hasTransform = geometry.hasTransform;
      node->transform = geometry.transform;
      if (!dirty_.count(node->wire)) geometry_.insert(node->wire);
    }
  }

  /** True while something is waiting to be sent (Zen may still lack credit). */
  bool pending() const {
    return enabled_ && (batchActive_ || resetPending_ || !dirty_.empty() || !geometry_.empty() || !events_.empty() ||
                        effectiveFocus() != sentFocus_);
  }
  size_t outstanding() const { return outstanding_.size(); }

  /**
   * Events to write now, at most the free credit. `target` is the exact cef-v1
   * target JSON; view size in logical points and its device scale select `px`.
   */
  std::vector<std::string> flush(const std::string& target, double viewWidth, double viewHeight, double scale, double nowMs) {
    std::vector<std::string> out;
    if (!enabled_) return out;
    while (outstanding_.size() < MaxOutstanding) {
      if (!batchActive_) {
        bool wanted = resetPending_ || !dirty_.empty() || !events_.empty() || effectiveFocus() != sentFocus_;
        if (!wanted || (!resetPending_ && nowMs - lastBatchMs_ < BatchIntervalMs)) break;
        batch_.assign(dirty_.begin(), dirty_.end());
        for (uint32_t wire : batch_) geometry_.erase(wire);
        dirty_.clear();
        batchPos_ = 0;
        batchActive_ = true;
        batchReset_ = resetPending_;
        resetPending_ = false;
        lastBatchMs_ = nowMs;
        batchNumber_++;
        pieces_.clear();
      }
      out.push_back(treeChunk(target, viewWidth, viewHeight, scale));
    }
    while (!batchActive_ && !geometry_.empty() && outstanding_.size() < MaxOutstanding) out.push_back(locationChunk(target));
    return out;
  }

  /** `ax_ack {seq}`: Released frees credit; Stale (from before a reset) is ignored. */
  Ack ack(uint64_t seq) {
    if (seq == 0 || seq > lastSeq_) return Ack::Unknown;
    auto found = std::find(outstanding_.begin(), outstanding_.end(), seq);
    if (found == outstanding_.end()) return Ack::Stale;
    outstanding_.erase(outstanding_.begin(), found + 1);
    return Ack::Released;
  }

  // ---- Lookups for ax_action (wire ids) ------------------------------------------------
  const Node* node(uint32_t wire) const {
    auto found = byWire_.find(wire);
    if (found == byWire_.end()) return nullptr;
    return find(trees_[found->second.first]->id, found->second.second);
  }
  uint32_t focus() const { return effectiveFocus(); }
  /** Absolute bounds in logical view points (what send_mouse_*_event takes). */
  bool rect(uint32_t wire, double viewWidth, double scale, Rect& out) const {
    auto found = byWire_.find(wire);
    if (found == byWire_.end()) return false;
    out = resolve(found->second.first, found->second.second);
    double px = pixelsPerPoint(viewWidth, scale);
    out = {out.x / px, out.y / px, out.w / px, out.h / px};
    return true;
  }
  /** Visible rect of the nearest scrollable ancestor (not the root), else false. */
  bool scrollContainer(uint32_t wire, double viewWidth, double scale, Rect& out) const {
    auto found = byWire_.find(wire);
    if (found == byWire_.end()) return false;
    const Tree& tree = *trees_[found->second.first];
    const Node* n = findIn(tree, found->second.second);
    for (int depth = 0; n && n->parent && depth < MaxDepth; depth++) {
      n = findIn(tree, n->parent);
      if (n && n->hasScroll && n->id != tree.root) return rect(n->wire, viewWidth, scale, out);
    }
    return false;
  }
  /** Rendering units: 1 when resolved bounds are view points, else the device scale. */
  double pixelsPerPoint(double viewWidth, double scale) const {
    if (rootTree_ < 0 || scale <= 1.01 || viewWidth <= 0) return 1;
    const Tree& tree = *trees_[size_t(rootTree_)];
    if (!findIn(tree, tree.root)) return 1;
    Rect root = resolve(size_t(rootTree_), tree.root);
    if (root.w <= 0) return 1;
    return std::fabs(root.w - viewWidth * scale) < std::fabs(root.w - viewWidth) ? scale : 1;
  }

 private:
  static constexpr int MaxDepth = 4096;
  struct Piece { uint32_t wire; std::string field; std::string text; std::vector<uint32_t> kids; };

  std::vector<std::unique_ptr<Tree>> trees_;
  std::unordered_map<std::string, size_t> treeIndex_;
  std::unordered_map<uint32_t, std::pair<size_t, int>> byWire_;
  std::unordered_map<std::string, std::pair<size_t, int>> hostOf_;  // child tree id -> iframe node
  // Trees of a replaced document. Only briefly: a back/forward-cache restore
  // brings the same tree id back later, and it must be accepted then.
  std::deque<std::pair<std::string, double>> retired_;
  double now_ = 0;
  long rootTree_ = -1;
  uint32_t nextWire_ = 1;
  size_t count_ = 0;
  bool enabled_ = false, truncated_ = false, resetPending_ = false;
  std::set<uint32_t> dirty_, geometry_;
  std::vector<std::pair<std::string, uint32_t>> events_;
  uint32_t sentFocus_ = 0;
  // Current batch.
  std::vector<uint32_t> batch_;
  size_t batchPos_ = 0;
  bool batchActive_ = false, batchReset_ = false;
  uint64_t batchNumber_ = 0;
  double lastBatchMs_ = -1e18;
  std::deque<Piece> pieces_;  // continuation records of the node just written
  // Flow control.
  uint64_t lastSeq_ = 0;
  std::deque<uint64_t> outstanding_;

  void clearAll() {
    trees_.clear();
    treeIndex_.clear();
    byWire_.clear();
    hostOf_.clear();
    rootTree_ = -1;
    count_ = 0;
    truncated_ = false;
    dirty_.clear();
    geometry_.clear();
    events_.clear();
    sentFocus_ = 0;
    batch_.clear();
    batchPos_ = 0;
    batchActive_ = false;
    pieces_.clear();
    resetPending_ = false;
  }
  void retire(const std::string& id) {
    retired_.push_back({id, now_});
    while (retired_.size() > MaxRetired) retired_.pop_front();
  }
  bool retiredRecently(const std::string& id) {
    while (!retired_.empty() && now_ - retired_.front().second > RetiredMs) retired_.pop_front();
    for (const auto& item : retired_)
      if (item.first == id) return true;
    return false;
  }
  const Node* findIn(const Tree& tree, int id) const {
    auto found = tree.nodes.find(id);
    return found == tree.nodes.end() ? nullptr : &found->second;
  }
  const Node* find(const std::string& treeId, int id) const {
    auto index = treeIndex_.find(treeId);
    return index == treeIndex_.end() || !trees_[index->second] ? nullptr : findIn(*trees_[index->second], id);
  }
  Node* findMutable(const std::string& treeId, int id) { return const_cast<Node*>(find(treeId, id)); }
  void markDirty(uint32_t wire) { dirty_.insert(wire); geometry_.erase(wire); }

  Tree* treeFor(const std::string& id) {
    auto found = treeIndex_.find(id);
    if (found != treeIndex_.end()) return trees_[found->second].get();
    if (treeIndex_.size() >= MaxTrees) { truncated_ = true; return nullptr; }
    // Slots of dropped trees are never reused, so stale indexes cannot alias.
    trees_.push_back(std::make_unique<Tree>());
    trees_.back()->id = id;
    treeIndex_[id] = trees_.size() - 1;
    return trees_.back().get();
  }
  void eraseNode(Tree& tree, std::unordered_map<int, Node>::iterator it) {
    byWire_.erase(it->second.wire);
    dirty_.erase(it->second.wire);
    geometry_.erase(it->second.wire);
    tree.nodes.erase(it);
    count_--;
  }
  void dropTree(size_t index) {
    Tree& tree = *trees_[index];
    while (!tree.nodes.empty()) eraseNode(tree, tree.nodes.begin());
    retire(tree.id);
    treeIndex_.erase(tree.id);
    if (rootTree_ == long(index)) rootTree_ = -1;
    trees_[index].reset();  // the slot stays empty; indexes are never reused
  }
  // A new main-frame tree replaced the document: drop the old root tree and every
  // tree below it, keep trees that already belong to the new one.
  void replaceRoot(size_t next) {
    std::set<std::string> gone;
    if (rootTree_ >= 0 && size_t(rootTree_) != next && trees_[size_t(rootTree_)]) gone.insert(trees_[size_t(rootTree_)]->id);
    for (bool grew = true; grew;) {
      grew = false;
      for (const auto& [id, index] : treeIndex_)
        if (!gone.count(id) && index != next && gone.count(trees_[index]->parentId)) { gone.insert(id); grew = true; }
    }
    for (const auto& id : gone) {
      auto index = treeIndex_.find(id);
      if (index != treeIndex_.end()) dropTree(index->second);
    }
    hostOf_.clear();
    for (const auto& [id, index] : treeIndex_)
      for (const auto& [nodeId, node] : trees_[index]->nodes)
        if (!node.childTree.empty()) hostOf_[node.childTree] = {index, nodeId};
    // Zen replaces its whole model with the next batch, which carries every node.
    resetPending_ = true;
    batchActive_ = false;
    batch_.clear();
    pieces_.clear();
    events_.clear();
    for (const auto& [wire, place] : byWire_) dirty_.insert(wire);
    geometry_.clear();
  }
  void clearDescendants(Tree& tree, int id) {
    auto found = tree.nodes.find(id);
    if (found == tree.nodes.end()) return;
    if (id == tree.root) {
      while (!tree.nodes.empty()) eraseNode(tree, tree.nodes.begin());
      tree.root = 0;
      return;
    }
    std::vector<int> stack(found->second.kids.begin(), found->second.kids.end());
    std::unordered_set<int> seen{id};
    while (!stack.empty()) {
      int next = stack.back();
      stack.pop_back();
      if (!seen.insert(next).second) continue;
      auto it = tree.nodes.find(next);
      if (it == tree.nodes.end()) continue;
      stack.insert(stack.end(), it->second.kids.begin(), it->second.kids.end());
      eraseNode(tree, it);
    }
    found = tree.nodes.find(id);
    if (found != tree.nodes.end()) { found->second.kids.clear(); markDirty(found->second.wire); }
  }
  void markHostDirty(const std::string& childTree) {
    auto host = hostOf_.find(childTree);
    if (host == hostOf_.end() || host->second.first >= trees_.size() || !trees_[host->second.first]) return;
    if (const Node* node = findIn(*trees_[host->second.first], host->second.second)) markDirty(node->wire);
  }

  void applyUpdate(const std::string& treeId, const Value& update) {
    Tree* tree = treeFor(treeId);
    if (!tree) return;
    size_t index = treeIndex_[treeId];
    const Value* hasData = update.get("has_tree_data");
    const Value* data = update.get("tree_data");
    if (hasData && hasData->type == Value::Type::Bool && hasData->boolean && data && data->isDict()) {
      const std::string* parent = stringField(*data, "parent_tree_id");
      tree->hasData = true;
      tree->parentId = parent && parent->size() <= 128 ? *parent : std::string();
      long long focus = 0;
      tree->focus = intField(*data, "focus_id", focus) && focus > 0 ? int(focus) : 0;
      const std::string* focused = stringField(*data, "focused_tree_id");
      tree->focusedTree = focused && focused->size() <= 128 ? *focused : std::string();
      if (tree->parentId.empty() && rootTree_ != long(index)) {
        if (rootTree_ >= 0) replaceRoot(index);
        rootTree_ = long(index);
      }
      if (!tree->parentId.empty()) markHostDirty(tree->id);  // link it under its iframe
    }
    long long clear = 0;
    if (intField(update, "node_id_to_clear", clear) && clear > 0) clearDescendants(*tree, int(clear));
    if (const Value* nodes = update.get("nodes"); nodes && nodes->isList())
      for (const auto& value : nodes->items) {
        long long id = 0;
        if (!value.isDict() || !intField(value, "id", id) || id <= 0) continue;
        const std::string* role = stringField(value, "role");
        if (role && *role == "inlineTextBox") continue;  // text-marker data; not exposed in v1
        auto found = tree->nodes.find(int(id));
        if (found == tree->nodes.end()) {
          if (count_ >= MaxNodes || nextWire_ == std::numeric_limits<uint32_t>::max()) { truncated_ = true; continue; }
          Node fresh;
          fresh.id = int(id);
          fresh.wire = nextWire_++;
          byWire_[fresh.wire] = {index, int(id)};
          count_++;
          found = tree->nodes.emplace(int(id), std::move(fresh)).first;
        }
        Node& n = found->second;
        std::string previousChild = n.childTree;
        parseNode(value, n);
        if (!n.childTree.empty()) {
          hostOf_[n.childTree] = {index, n.id};
        }
        if (previousChild != n.childTree && !previousChild.empty()) hostOf_.erase(previousChild);
        markDirty(n.wire);
      }
    long long root = 0;
    if (intField(update, "root_id", root) && root > 0 && tree->root != int(root)) {
      tree->root = int(root);
      if (!tree->parentId.empty()) markHostDirty(tree->id);
    }
    collect(*tree);
  }
  // Nodes no longer reachable from the tree root are gone (AXTree semantics:
  // a child dropped from every child list without reappearing is deleted).
  void collect(Tree& tree) {
    auto root = tree.nodes.find(tree.root);
    if (root == tree.nodes.end()) return;
    std::unordered_set<int> seen{tree.root};
    std::vector<int> stack{tree.root};
    root->second.parent = 0;
    if (root->second.hidden) { root->second.hidden = false; markDirty(root->second.wire); }
    while (!stack.empty()) {
      Node& n = tree.nodes.find(stack.back())->second;
      stack.pop_back();
      bool hideKids = n.hidden || n.isProtected();
      for (int kid : n.kids) {
        auto it = tree.nodes.find(kid);
        if (it == tree.nodes.end() || !seen.insert(kid).second) continue;
        // A child attached later than its parent's record: the parent's kids change.
        if (it->second.parent != n.id) { it->second.parent = n.id; markDirty(n.wire); }
        if (it->second.hidden != hideKids) { it->second.hidden = hideKids; markDirty(it->second.wire); }
        stack.push_back(kid);
      }
    }
    for (auto it = tree.nodes.begin(); it != tree.nodes.end();) {
      if (seen.count(it->first)) { ++it; continue; }
      auto next = std::next(it);
      eraseNode(tree, it);
      it = next;
    }
  }

  uint32_t effectiveFocus() const {
    if (rootTree_ < 0 || !trees_[size_t(rootTree_)]) return 0;
    const Tree* tree = trees_[size_t(rootTree_)].get();
    if (!tree->focusedTree.empty()) {
      auto found = treeIndex_.find(tree->focusedTree);
      if (found != treeIndex_.end() && trees_[found->second]) tree = trees_[found->second].get();
    }
    const Node* n = findIn(*tree, tree->focus);
    return n && !n->hidden ? n->wire : 0;
  }
  // Chromium AXTree::RelativeToTreeBounds: apply the node's transform, offset by
  // its container (offset container, else the tree root), minus the container's
  // scroll, repeat. A child tree's root continues at its iframe host node.
  Rect resolve(size_t treeIndex, int id) const {
    const Tree* tree = trees_[treeIndex].get();
    const Node* n = findIn(*tree, id);
    if (!n) return {};
    Rect r = n->local;
    for (int depth = 0; n && depth < MaxDepth; depth++) {
      if (n->hasTransform) r = mapRect(n->transform, r);
      const Node* container = nullptr;
      if (n->offsetContainer && n->offsetContainer != n->id) container = findIn(*tree, n->offsetContainer);
      if (!container && n->id != tree->root) container = findIn(*tree, tree->root);
      if (!container) {
        auto host = hostOf_.find(tree->id);
        if (n->id != tree->root || host == hostOf_.end() || !trees_[host->second.first]) break;
        tree = trees_[host->second.first].get();
        container = findIn(*tree, host->second.second);
        if (!container) break;
      }
      if (container == n) break;
      r.x += container->local.x;
      r.y += container->local.y;
      if (container->hasScroll) { r.x -= container->scrollX; r.y -= container->scrollY; }
      n = container;
    }
    return r;
  }
  // Wire id of the node's effective offset container (see resolve()).
  uint32_t containerWire(const Tree& tree, const Node& n) const {
    if (n.offsetContainer && n.offsetContainer != n.id)
      if (const Node* c = findIn(tree, n.offsetContainer)) return c->wire;
    if (n.id != tree.root)
      if (const Node* c = findIn(tree, tree.root)) return c->wire;
    auto host = hostOf_.find(tree.id);
    if (host != hostOf_.end() && trees_[host->second.first])
      if (const Node* c = findIn(*trees_[host->second.first], host->second.second)) return c->wire;
    return 0;
  }
  std::vector<uint32_t> kidsOf(const Tree& tree, const Node& n) const {
    std::vector<uint32_t> kids;
    if (n.isProtected()) return kids;
    for (int kid : n.kids)
      if (const Node* k = findIn(tree, kid); k && !k->hidden && k->parent == n.id) kids.push_back(k->wire);
    if (!n.childTree.empty()) {
      auto child = treeIndex_.find(n.childTree);
      if (child != treeIndex_.end() && trees_[child->second] && trees_[child->second]->parentId == tree.id)
        if (const Node* root = findIn(*trees_[child->second], trees_[child->second]->root)) kids.push_back(root->wire);
    }
    return kids;
  }

  void geometryFields(std::string& out, const Tree& tree, const Node& n) const {
    out += ",\"b\":[";
    appendNumber(out, n.local.x); out += ',';
    appendNumber(out, n.local.y); out += ',';
    appendNumber(out, n.local.w); out += ',';
    appendNumber(out, n.local.h); out += ']';
    out += ",\"oc\":" + std::to_string(containerWire(tree, n));
    if (n.hasTransform) {
      const double m[6] = {n.transform.a, n.transform.b, n.transform.c, n.transform.d, n.transform.e, n.transform.f};
      out += ",\"tf\":[";
      for (int i = 0; i < 6; i++) { if (i) out += ','; appendNumber(out, m[i]); }
      out += ']';
    }
  }
  // Base record without `kids`; `full` false gives the minimal fallback record.
  std::string baseRecord(const Tree& tree, const Node& n, bool full, std::deque<Piece>& pieces) const {
    std::string out = "{\"id\":" + std::to_string(n.wire) + ",\"role\":";
    appendJson(out, n.role);
    if (n.states) {
      size_t count = 0;
      const char* const* names = stateNames(count);
      out += ",\"states\":[";
      bool first = true;
      for (size_t i = 0; i < count; i++)
        if (n.states & (1u << i)) { if (!first) out += ','; first = false; appendJson(out, names[i]); }
      out += ']';
    }
    geometryFields(out, tree, n);
    if (n.hasScroll) {
      out += ",\"scroll\":[";
      appendNumber(out, n.scrollX); out += ',';
      appendNumber(out, n.scrollY); out += ']';
    }
    bool redacted = n.isProtected();
    if (redacted) out += ",\"redacted\":true";
    if (!full) return out;
    if (!n.actions.empty()) {
      out += ",\"actions\":[";
      for (size_t i = 0; i < n.actions.size(); i++) { if (i) out += ','; appendJson(out, n.actions[i]); }
      out += ']';
    }
    auto field = [&](const char* key, const std::string& value) {
      if (value.empty()) return;
      out += ",\""; out += key; out += "\":";
      appendJson(out, value);
    };
    auto longText = [&](const char* key, const std::string& value, size_t first, size_t rest) {
      if (value.empty()) return;
      std::vector<std::string> parts = splitText(value, first, rest);
      field(key, parts[0]);
      for (size_t i = 1; i < parts.size(); i++) pieces.push_back(Piece{n.wire, key, parts[i], {}});
    };
    longText("name", n.name, NamePiece, 2048);
    if (!redacted) longText("value", n.value, ValuePiece, 2048);
    field("desc", n.desc);
    field("placeholder", n.placeholder);
    field("url", n.url);
    field("roledesc", n.roledesc);
    field("shortcuts", n.shortcuts);
    field("lang", n.lang);
    field("checked", n.checked);
    field("invalid", n.invalid);
    field("restriction", n.restriction);
    field("live", n.live);
    field("relevant", n.relevant);
    field("current", n.current);
    field("input", n.input);
    field("action", n.action);
    field("tag", n.tag);
    auto integer = [&](const char* key, long long value) {
      if (value < 0) return;
      out += ",\""; out += key; out += "\":" + std::to_string(std::min<long long>(value, 1000000000));
    };
    integer("level", n.level);
    integer("popup", n.popup);
    integer("setsize", n.setsize);
    integer("posinset", n.posinset);
    if (!redacted && n.selStart >= 0 && n.selEnd >= 0)
      out += ",\"sel\":[" + std::to_string(std::min<long long>(n.selStart, 1000000000)) + "," +
             std::to_string(std::min<long long>(n.selEnd, 1000000000)) + "]";
    if (n.hasRange) {
      out += ",\"range\":[";
      for (int i = 0; i < 4; i++) {
        if (i) out += ',';
        if (std::isnan(n.range[i])) out += "null"; else appendNumber(out, n.range[i]);
      }
      out += ']';
    }
    if (std::any_of(std::begin(n.table), std::end(n.table), [](long long v) { return v >= 0; })) {
      out += ",\"table\":[";
      for (int i = 0; i < 6; i++) { if (i) out += ','; out += std::to_string(std::clamp<long long>(n.table[i], -1, 1000000)); }
      out += ']';
    }
    auto flag = [&](const char* key, int8_t value) {
      if (value < 0) return;
      out += ",\""; out += key; out += value ? "\":true" : "\":false";
    };
    flag("atomic", n.atomic);
    flag("busy", n.busy);
    flag("selected", n.selected);
    flag("modal", n.modal);
    auto reference = [&](const char* key, long long id) {
      if (id <= 0) return;
      const Node* target = findIn(tree, int(id));
      if (target && !target->hidden) { out += ",\""; out += key; out += "\":" + std::to_string(target->wire); }
    };
    reference("activedesc", n.activedesc);
    reference("linktarget", n.linktarget);
    return out;
  }
  static void appendIds(std::string& out, const std::vector<uint32_t>& ids, size_t from, size_t to) {
    out += '[';
    for (size_t i = from; i < to; i++) { if (i > from) out += ','; out += std::to_string(ids[i]); }
    out += ']';
  }
  // How many ids from `from` fit in `budget` bytes as a JSON array.
  static size_t idsFitting(const std::vector<uint32_t>& ids, size_t from, size_t budget) {
    size_t used = 2, n = 0;
    for (size_t i = from; i < ids.size(); i++) {
      size_t add = std::to_string(ids[i]).size() + (n ? 1 : 0);
      if (used + add > budget) break;
      used += add;
      n++;
    }
    return n;
  }
  std::string pieceRecord(const Piece& piece) const {
    std::string out = "{\"id\":" + std::to_string(piece.wire) + ",\"append\":";
    appendJson(out, piece.field);
    if (piece.field == "kids") { out += ",\"kids\":"; appendIds(out, piece.kids, 0, piece.kids.size()); }
    else { out += ",\"text\":"; appendJson(out, piece.text); }
    return out + "}";
  }

  std::string treeChunk(const std::string& target, double viewWidth, double viewHeight, double scale) {
    (void)viewHeight;
    uint64_t seq = ++lastSeq_;
    std::string head = "{\"event\":\"ax_tree_update\",\"version\":1,\"target\":" + target + ",\"seq\":" + std::to_string(seq) +
                       ",\"batch\":" + std::to_string(batchNumber_) + ",\"reset\":" + (batchReset_ ? "true" : "false") + ",\"nodes\":[";
    // The final tail is reserved in every chunk, whichever chunk ends the batch.
    uint32_t focus = effectiveFocus();
    uint32_t root = 0;
    if (rootTree_ >= 0 && trees_[size_t(rootTree_)])
      if (const Node* r = findIn(*trees_[size_t(rootTree_)], trees_[size_t(rootTree_)]->root)) root = r->wire;
    std::string finalTail = "],\"final\":true,\"root\":" + std::to_string(root) + ",\"focus\":" + std::to_string(focus) + ",\"px\":";
    appendNumber(finalTail, pixelsPerPoint(viewWidth, scale));
    finalTail += ",\"events\":[";
    for (size_t i = 0; i < events_.size(); i++) {
      if (i) finalTail += ',';
      finalTail += "{\"type\":";
      appendJson(finalTail, events_[i].first);
      finalTail += ",\"id\":" + std::to_string(events_[i].second) + "}";
    }
    finalTail += std::string("],\"truncated\":") + (truncated_ ? "true" : "false") + "}";
    const std::string openTail = "],\"final\":false,\"root\":0,\"focus\":0,\"px\":1,\"events\":[],\"truncated\":false}";
    size_t limit = MaxEventBytes - EventSlack;
    size_t budget = limit > head.size() + finalTail.size() ? limit - head.size() - finalTail.size() : 0;
    std::string body;
    auto add = [&](const std::string& record) -> bool {
      size_t extra = record.size() + (body.empty() ? 0 : 1);
      if (extra > budget) return false;
      if (!body.empty()) body += ',';
      body += record;
      budget -= extra;
      return true;
    };
    while (true) {
      if (!pieces_.empty()) {
        Piece& piece = pieces_.front();
        if (piece.field == "kids") {
          // Fill what is left of this chunk; the rest stays queued.
          size_t overhead = 40 + std::to_string(piece.wire).size();
          size_t fit = budget > overhead ? idsFitting(piece.kids, 0, budget - overhead) : 0;
          if (fit == 0) { if (body.empty()) fit = 1; else break; }
          Piece slice{piece.wire, "kids", {}, std::vector<uint32_t>(piece.kids.begin(), piece.kids.begin() + long(fit))};
          if (!add(pieceRecord(slice))) break;
          piece.kids.erase(piece.kids.begin(), piece.kids.begin() + long(fit));
          if (piece.kids.empty()) pieces_.pop_front();
        } else {
          if (!add(pieceRecord(piece))) break;
          pieces_.pop_front();
        }
        continue;
      }
      if (batchPos_ >= batch_.size()) break;
      uint32_t wire = batch_[batchPos_];
      auto place = byWire_.find(wire);
      const Node* n = place == byWire_.end() ? nullptr : findIn(*trees_[place->second.first], place->second.second);
      if (!n || n->hidden) { batchPos_++; continue; }  // removed or inside a password field
      const Tree& tree = *trees_[place->second.first];
      std::vector<uint32_t> kids = kidsOf(tree, *n);
      std::deque<Piece> pieces;
      std::string base = baseRecord(tree, *n, true, pieces);
      size_t room = budget > base.size() + 12 + (body.empty() ? 0 : 1) ? budget - base.size() - 12 - (body.empty() ? 0 : 1) : 0;
      if (base.size() + 12 > budget) {
        if (!body.empty()) break;  // next chunk
        pieces.clear();
        base = baseRecord(tree, *n, false, pieces);  // cannot happen with the budgets above; stay valid anyway
        room = budget > base.size() + 13 ? budget - base.size() - 13 : 0;
      }
      size_t fit = idsFitting(kids, 0, room);
      std::string record = base + ",\"kids\":";
      appendIds(record, kids, 0, fit);
      record += "}";
      if (!add(record)) break;
      batchPos_++;
      for (auto& piece : pieces) pieces_.push_back(std::move(piece));
      if (fit < kids.size()) pieces_.push_back(Piece{wire, "kids", {}, std::vector<uint32_t>(kids.begin() + long(fit), kids.end())});
    }
    bool final = batchPos_ >= batch_.size() && pieces_.empty();
    std::string event = head + body + (final ? finalTail : openTail);
    if (final) {
      batchActive_ = false;
      batch_.clear();
      sentFocus_ = focus;
      events_.clear();
    }
    outstanding_.push_back(seq);
    return event;
  }

  std::string locationChunk(const std::string& target) {
    uint64_t seq = ++lastSeq_;
    std::string head = "{\"event\":\"ax_location\",\"version\":1,\"target\":" + target + ",\"seq\":" + std::to_string(seq) + ",\"nodes\":[";
    size_t limit = MaxEventBytes - EventSlack;
    size_t budget = limit > head.size() + 2 ? limit - head.size() - 2 : 0;
    std::string body;
    while (!geometry_.empty()) {
      uint32_t wire = *geometry_.begin();
      auto place = byWire_.find(wire);
      const Node* n = place == byWire_.end() ? nullptr : findIn(*trees_[place->second.first], place->second.second);
      if (!n || n->hidden) { geometry_.erase(geometry_.begin()); continue; }
      std::string record = "{\"id\":" + std::to_string(wire);
      geometryFields(record, *trees_[place->second.first], *n);
      record += "}";
      size_t extra = record.size() + (body.empty() ? 0 : 1);
      if (extra > budget) break;
      if (!body.empty()) body += ',';
      body += record;
      budget -= extra;
      geometry_.erase(geometry_.begin());
    }
    outstanding_.push_back(seq);
    return head + body + "]}";
  }
};

}  // namespace axio::ax
