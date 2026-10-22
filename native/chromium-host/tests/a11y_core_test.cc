// Standalone unit test for native/chromium-host/a11y_core.hpp (no CEF, no AppKit).
//
//   clang++ -std=c++17 -Wall -Wextra -I native/chromium-host \
//     native/chromium-host/tests/a11y_core_test.cc -o "$AXIO_BUILD/a11y_core_test" && "$AXIO_BUILD/a11y_core_test"
//
// Build output belongs on the project build volume (AGENTS.md); run it through
// dev-external + scripts/storage.py exec like every other native command.
// Fixtures follow the CEF 062ebe4 OSR serializer (osr_accessibility_util.cc,
// unchanged in the pinned 47dbc1d):
// {ax_tree_id, updates:[{has_tree_data, tree_data, node_id_to_clear, root_id,
// nodes:[{id, role, state, location, child_ids, attributes, ...}]}], events}.
#include "a11y_core.hpp"

#include <cassert>
#include <cstdio>
#include <functional>
#include <map>
#include <string>
#include <vector>

using axio::ax::Mirror;
using axio::ax::Value;

// ---- Minimal JSON parser (test only) --------------------------------------------------
struct Parser {
  const std::string& s;
  size_t i = 0;
  bool ok = true;
  void ws() { while (i < s.size() && (s[i] == ' ' || s[i] == '\n' || s[i] == '\t' || s[i] == '\r')) i++; }
  Value parse() {
    ws();
    Value v;
    if (i >= s.size()) { ok = false; return v; }
    char c = s[i];
    if (c == '{') {
      v.type = Value::Type::Dict; i++; ws();
      if (s[i] == '}') { i++; return v; }
      while (ok) {
        ws(); Value key = parse(); ws();
        if (key.type != Value::Type::String || s[i] != ':') { ok = false; break; }
        i++;
        Value item = parse();
        v.fields.push_back({key.text, item});
        ws();
        if (s[i] == ',') { i++; continue; }
        if (s[i] == '}') { i++; break; }
        ok = false;
      }
    } else if (c == '[') {
      v.type = Value::Type::List; i++; ws();
      if (s[i] == ']') { i++; return v; }
      while (ok) {
        v.items.push_back(parse()); ws();
        if (s[i] == ',') { i++; continue; }
        if (s[i] == ']') { i++; break; }
        ok = false;
      }
    } else if (c == '"') {
      v.type = Value::Type::String; i++;
      while (i < s.size() && s[i] != '"') {
        if (s[i] == '\\') {
          char e = s[++i];
          if (e == 'n') v.text += '\n'; else if (e == 't') v.text += '\t'; else if (e == 'r') v.text += '\r';
          else if (e == 'b') v.text += '\b'; else if (e == 'f') v.text += '\f';
          else if (e == 'u') { unsigned cp = std::stoul(s.substr(i + 1, 4), nullptr, 16); axio::ax::appendUtf8(v.text, cp); i += 4; }
          else v.text += e;
          i++;
        } else v.text += s[i++];
      }
      if (i >= s.size()) ok = false; else i++;
    } else if (s.compare(i, 4, "true") == 0) { v.type = Value::Type::Bool; v.boolean = true; i += 4; }
    else if (s.compare(i, 5, "false") == 0) { v.type = Value::Type::Bool; i += 5; }
    else if (s.compare(i, 4, "null") == 0) { i += 4; }
    else {
      char* end = nullptr;
      v.type = Value::Type::Number; v.number = std::strtod(s.c_str() + i, &end);
      if (end == s.c_str() + i) ok = false;
      i = size_t(end - s.c_str());
    }
    return v;
  }
};
static Value json(const std::string& text) {
  Parser p{text};
  Value v = p.parse();
  if (!p.ok) { std::fprintf(stderr, "bad fixture JSON: %s\n", text.c_str()); std::abort(); }
  return v;
}

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { std::fprintf(stderr, "%s:%d: CHECK(%s)\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

static const std::string Target = R"({"document_generation":3,"engine":"chromium","engine_instance":"cef-1","identity":"https://example.test","native_target_id":"7","navigation_generation":3,"private_mode":false,"tab_id":"tab-1"})";

// Applies every emitted event to a tiny Zen-side model, like ChromiumAccessibility.sys.mjs.
struct Zen {
  std::map<long long, Value> nodes, staged;
  long long root = 0, focus = 0, lastBatch = -1;
  double px = 1;
  bool reset = false, truncated = false;
  std::vector<std::string> events;
  std::vector<uint64_t> seqs;
  void apply(const std::string& line) {
    CHECK(line.size() <= axio::ax::MaxEventBytes);
    Value e = json(line);
    long long seq = 0;
    CHECK(axio::ax::intField(e, "seq", seq));
    seqs.push_back(uint64_t(seq));
    CHECK(e.get("target") && e.get("target")->isDict());
    const std::string* kind = axio::ax::stringField(e, "event");
    if (*kind == "ax_location") {
      for (const auto& n : e.get("nodes")->items) {
        long long id = 0; axio::ax::intField(n, "id", id);
        auto found = nodes.find(id);
        if (found == nodes.end()) continue;
        for (auto& field : found->second.fields)
          if (field.first == "b" || field.first == "oc" || field.first == "tf") field.second = *n.get(field.first.c_str());
      }
      return;
    }
    long long batch = 0; axio::ax::intField(e, "batch", batch);
    if (batch != lastBatch) { staged.clear(); lastBatch = batch; reset = false; }
    if (e.get("reset")->boolean) reset = true;
    for (const auto& n : e.get("nodes")->items) {
      long long id = 0; axio::ax::intField(n, "id", id);
      if (const std::string* append = axio::ax::stringField(n, "append")) {
        Value& target = staged[id];
        for (auto& field : target.fields) {
          if (field.first != *append) continue;
          if (*append == "kids") for (const auto& k : n.get("kids")->items) field.second.items.push_back(k);
          else field.second.text += n.get("text")->text;
        }
        continue;
      }
      staged[id] = n;
    }
    if (!e.get("final")->boolean) return;
    if (reset) nodes.clear();
    for (auto& [id, n] : staged) nodes[id] = n;
    staged.clear();
    axio::ax::intField(e, "root", root);
    axio::ax::intField(e, "focus", focus);
    e.get("px")->toDouble(px);
    truncated = e.get("truncated")->boolean;
    for (const auto& item : e.get("events")->items) events.push_back(item.get("type")->text);
    // Reachability GC, as the JS model does.
    std::map<long long, bool> seen;
    std::function<void(long long)> walk = [&](long long id) {
      if (seen[id] || !nodes.count(id)) return;
      seen[id] = true;
      if (const Value* kids = nodes[id].get("kids")) for (const auto& k : kids->items) walk((long long)k.number);
    };
    walk(root);
    for (auto it = nodes.begin(); it != nodes.end();) it = seen[it->first] ? std::next(it) : nodes.erase(it);
  }
  const Value* byName(const std::string& name) const {
    for (const auto& [id, n] : nodes) if (const std::string* s = axio::ax::stringField(n, "name"); s && *s == name) return &n;
    return nullptr;
  }
  long long idOf(const std::string& name) const {
    const Value* n = byName(name);
    long long id = 0;
    if (n) axio::ax::intField(*n, "id", id);
    return id;
  }
};

// Drains the mirror completely, acknowledging every event like Zen does.
static void pump(Mirror& m, Zen& zen, double& now, double width = 800, double height = 600, double scale = 2) {
  for (int round = 0; round < 1000 && m.pending(); round++) {
    now += axio::ax::BatchIntervalMs;
    auto lines = m.flush(Target, width, height, scale, now);
    for (const auto& line : lines) zen.apply(line);
    for (uint64_t seq : zen.seqs) CHECK(m.ack(seq) == Mirror::Ack::Released);
    zen.seqs.clear();
  }
  CHECK(!m.pending());
}

static std::string page(const std::string& nodes, int root = 1, const std::string& extra = "") {
  return R"({"ax_tree_id":"main","updates":[{"has_tree_data":true,"tree_data":{"tree_id":"main","focus_id":3,"loaded":true},"root_id":)" +
         std::to_string(root) + R"(,"nodes":[)" + nodes + "]" + extra + "}]}";
}
static const std::string Basic =
    R"({"id":1,"role":"rootWebArea","state":["focusable"],"location":{"x":0,"y":0,"width":800,"height":600},"child_ids":[2,3,4],"attributes":{"name":"Fixture","scrollX":0,"scrollY":0}},)"
    R"({"id":2,"role":"heading","location":{"x":10,"y":10,"width":200,"height":30},"child_ids":[5],"attributes":{"name":"Welcome","hierarchicalLevel":1}},)"
    R"({"id":5,"role":"staticText","location":{"x":10,"y":10,"width":200,"height":30},"child_ids":[6],"attributes":{"name":"Welcome"}},)"
    R"({"id":6,"role":"inlineTextBox","location":{"x":10,"y":10,"width":200,"height":30},"attributes":{"name":"Welcome"}},)"
    R"({"id":3,"role":"textField","state":["editable","focusable"],"location":{"x":10,"y":50,"width":300,"height":24},"actions":["focus","setValue","bogus"],"attributes":{"name":"Email","value":"me@example.test","textSelStart":2,"textSelEnd":2}},)"
    R"({"id":4,"role":"textField","state":["editable","focusable","protected"],"location":{"x":10,"y":90,"width":300,"height":24},"child_ids":[7],"attributes":{"name":"Password","value":"hunter2","inputType":"password","textSelStart":7,"textSelEnd":7}},)"
    R"({"id":7,"role":"staticText","location":{"x":10,"y":90,"width":80,"height":24},"attributes":{"name":"hunter2"}})";

static void testBasicTreeAndRedaction() {
  Mirror m; Zen zen; double now = 0;
  m.reset();
  m.treeChange(json(page(Basic)), now);
  pump(m, zen, now);
  CHECK(zen.root != 0);
  CHECK(zen.nodes.size() == 5);  // root, heading, text, field, password (no inline box, no password text)
  const Value* email = zen.byName("Email");
  CHECK(email && axio::ax::stringField(*email, "value") && *axio::ax::stringField(*email, "value") == "me@example.test");
  CHECK(email && email->get("sel"));
  CHECK(email && email->get("actions") && email->get("actions")->items.size() == 2);  // "bogus" dropped
  CHECK(zen.focus == zen.idOf("Email"));
  const Value* password = zen.byName("Password");
  CHECK(password && !password->get("value") && !password->get("sel"));
  CHECK(password && password->get("redacted") && password->get("redacted")->boolean);
  CHECK(password && password->get("kids")->items.empty());
  CHECK(!zen.byName("hunter2"));
  // The secret appears nowhere in any emitted byte.
  Mirror again; again.reset(); again.treeChange(json(page(Basic)), 0);
  for (const auto& line : again.flush(Target, 800, 600, 2, 1e6)) CHECK(line.find("hunter2") == std::string::npos);
  const Value* heading = zen.byName("Welcome");
  long long level = 0;
  CHECK(heading && axio::ax::intField(*heading, "level", level) && level == 1);
}

static void testDeletionAndReparenting() {
  Mirror m; Zen zen; double now = 0;
  m.reset();
  m.treeChange(json(page(Basic)), now);
  pump(m, zen, now);
  long long text = zen.idOf("Email");
  // Move the email field under a new group (reparent) and drop the heading.
  m.treeChange(json(R"({"ax_tree_id":"main","updates":[{"nodes":[)"
      R"({"id":1,"role":"rootWebArea","location":{"x":0,"y":0,"width":800,"height":600},"child_ids":[8,4]},)"
      R"({"id":8,"role":"group","location":{"x":0,"y":40,"width":400,"height":40},"child_ids":[3],"attributes":{"name":"Box"}},)"
      R"({"id":3,"role":"textField","state":["editable","focusable"],"location":{"x":10,"y":10,"width":300,"height":24},"offset_container_id":8,"attributes":{"name":"Email","value":"new"}})"
      R"(]}]})"), now);
  pump(m, zen, now);
  CHECK(!zen.byName("Welcome"));
  CHECK(zen.idOf("Email") == text);  // same wire id after reparenting
  const Value* box = zen.byName("Box");
  CHECK(box && box->get("kids")->items.size() == 1 && (long long)box->get("kids")->items[0].number == text);
  CHECK(m.nodeCount() == 5);  // root, box, email, password and its hidden text (heading subtree collected)
  // node_id_to_clear empties a subtree.
  m.treeChange(json(R"({"ax_tree_id":"main","updates":[{"node_id_to_clear":8,"nodes":[{"id":8,"role":"group","location":{"x":0,"y":40,"width":400,"height":40},"attributes":{"name":"Box"}}]}]})"), now);
  pump(m, zen, now);
  CHECK(!zen.byName("Email"));
  CHECK(m.nodeCount() == 4);
}

static void testBoundsResolution() {
  Mirror m; double now = 0;
  m.reset();
  // Physical-pixel bounds (no root transform): px == device scale. The list
  // scrolls by 100, its item is offset inside it, the root page scrolls by 50.
  m.treeChange(json(page(
      R"({"id":1,"role":"rootWebArea","location":{"x":0,"y":0,"width":1600,"height":1200},"child_ids":[2],"attributes":{"scrollX":0,"scrollY":50}},)"
      R"({"id":2,"role":"list","location":{"x":100,"y":400,"width":600,"height":300},"child_ids":[3],"attributes":{"scrollX":0,"scrollY":100}},)"
      R"({"id":3,"role":"listItem","offset_container_id":2,"location":{"x":20,"y":120,"width":200,"height":40},"attributes":{"name":"Item"}})")), now);
  auto lines = m.flush(Target, 800, 600, 2, now + 1000);
  CHECK(lines.size() == 1);
  Value e = json(lines[0]);
  double px = 0; e.get("px")->toDouble(px);
  CHECK(px == 2);
  axio::ax::Rect r;
  CHECK(m.rect(3, 800, 2, r));
  // x: 20 + 100 - 0 + 0 = 120 phys -> 60 pt ; y: 120 + 400 - 100 + 0 - 50 = 370 phys -> 185 pt
  CHECK(std::fabs(r.x - 60) < 1e-9 && std::fabs(r.y - 185) < 1e-9 && std::fabs(r.w - 100) < 1e-9 && std::fabs(r.h - 20) < 1e-9);
  // A root transform scaling by 1/dsf yields view points: px == 1.
  Mirror scaled; scaled.reset();
  scaled.treeChange(json(page(
      R"({"id":1,"role":"rootWebArea","transform":"[ +0.5000 +0.0000 +0.0000 +0.0000  \n  +0.0000 +0.5000 +0.0000 +0.0000  \n  +0.0000 +0.0000 +1.0000 +0.0000  \n  +0.0000 +0.0000 +0.0000 +1.0000 ]\n","location":{"x":0,"y":0,"width":1600,"height":1200},"child_ids":[2]},)"
      R"({"id":2,"role":"button","location":{"x":200,"y":100,"width":100,"height":40},"attributes":{"name":"Go"}})")), 0);
  Value s = json(scaled.flush(Target, 800, 600, 2, 1000)[0]);
  s.get("px")->toDouble(px);
  CHECK(px == 1);
  CHECK(scaled.rect(2, 800, 2, r));
  // The button is offset by the root, then the root's transform scales: Chromium
  // applies each container's transform after offsetting into it.
  CHECK(std::fabs(r.x - 100) < 1e-9 && std::fabs(r.y - 50) < 1e-9 && std::fabs(r.w - 50) < 1e-9);
}

static void testChunkingAndContinuations() {
  Mirror m; Zen zen; double now = 0;
  m.reset();
  std::string nodes = R"({"id":1,"role":"rootWebArea","location":{"x":0,"y":0,"width":800,"height":600},"child_ids":[)";
  for (int i = 2; i < 2002; i++) nodes += (i > 2 ? "," : "") + std::to_string(i);
  nodes += "]}";
  for (int i = 2; i < 2002; i++)
    nodes += R"(,{"id":)" + std::to_string(i) + R"(,"role":"staticText","location":{"x":0,"y":)" + std::to_string(i) +
             R"(,"width":10,"height":10},"attributes":{"name":")" + (i == 2 ? std::string(20000, 'b') : "t" + std::to_string(i)) + "\"}}";
  m.treeChange(json(page(nodes)), now);
  CHECK(m.nodeCount() == 2001);
  size_t events = 0;
  for (int round = 0; round < 1000 && m.pending(); round++) {
    now += 200;
    auto lines = m.flush(Target, 800, 600, 2, now);
    CHECK(lines.size() <= axio::ax::MaxOutstanding);
    CHECK(m.outstanding() <= axio::ax::MaxOutstanding);
    // Without acks nothing more is written.
    CHECK(m.flush(Target, 800, 600, 2, now + 1).empty());
    for (const auto& line : lines) { zen.apply(line); events++; }
    for (uint64_t seq : zen.seqs) CHECK(m.ack(seq) == Mirror::Ack::Released);
    zen.seqs.clear();
  }
  CHECK(events > 4);
  const Value* root = nullptr;
  for (const auto& [id, n] : zen.nodes) if (n.get("role")->text == "rootWebArea") root = &n;
  CHECK(root && root->get("kids")->items.size() == 2000);
  const Value* big = zen.byName(std::string(16384 - 3, 'b') + "\xe2\x80\xa6");
  CHECK(big != nullptr);  // name clipped to its total budget, joined from append records
  CHECK(zen.nodes.size() == 2001);
}

static void testFlowControlAndAcks() {
  Mirror m; double now = 0;
  m.reset();
  m.treeChange(json(page(Basic)), now);
  auto lines = m.flush(Target, 800, 600, 2, 1000);
  CHECK(lines.size() == 1);
  CHECK(m.ack(99) == Mirror::Ack::Unknown);  // never sent: protocol error in the host
  CHECK(m.ack(1) == Mirror::Ack::Released);
  CHECK(m.ack(1) == Mirror::Ack::Stale);
  m.disable();
  CHECK(m.flush(Target, 800, 600, 2, 5000).empty());
  m.reset();
  m.treeChange(json(page(Basic)), 6000);
  lines = m.flush(Target, 800, 600, 2, 6000);
  CHECK(lines.size() == 1 && json(lines[0]).get("reset")->boolean);
  CHECK(json(lines[0]).get("seq")->number == 2);  // seq keeps rising across epochs
}

static void testLocationChanges() {
  Mirror m; Zen zen; double now = 0;
  m.reset();
  m.treeChange(json(page(Basic)), now);
  pump(m, zen, now);
  m.locationChange(json(R"([{"ax_tree_id":"main","id":3,"new_location":{"bounds":{"x":11,"y":51,"width":300,"height":24}}},{"ax_tree_id":"other","id":3,"new_location":{"bounds":{"x":0,"y":0,"width":1,"height":1}}}])"));
  auto lines = m.flush(Target, 800, 600, 2, now + 1);  // geometry is not throttled
  CHECK(lines.size() == 1);
  Value e = json(lines[0]);
  CHECK(e.get("event")->text == "ax_location");
  CHECK(e.get("nodes")->items.size() == 1);
  zen.apply(lines[0]);
  const Value* email = zen.byName("Email");
  CHECK(email && email->get("b")->items[0].number == 11);
}

static void testChildTreesAndNavigation() {
  Mirror m; Zen zen; double now = 0;
  m.reset();
  m.treeChange(json(page(
      R"({"id":1,"role":"rootWebArea","location":{"x":0,"y":0,"width":800,"height":600},"child_ids":[2]},)"
      R"({"id":2,"role":"iframe","location":{"x":50,"y":60,"width":300,"height":200},"attributes":{"childTreeId":"child"}})")), now);
  m.treeChange(json(R"({"ax_tree_id":"child","updates":[{"has_tree_data":true,"tree_data":{"tree_id":"child","parent_tree_id":"main","focus_id":2},"root_id":1,"nodes":[)"
      R"({"id":1,"role":"rootWebArea","location":{"x":0,"y":0,"width":300,"height":200},"child_ids":[2]},)"
      R"({"id":2,"role":"button","location":{"x":5,"y":6,"width":40,"height":20},"attributes":{"name":"Inner"}}]}]})"), now);
  pump(m, zen, now, 800, 600, 1);
  long long inner = zen.idOf("Inner");
  CHECK(inner != 0);
  axio::ax::Rect r;
  CHECK(m.rect(uint32_t(inner), 800, 1, r));
  CHECK(r.x == 55 && r.y == 66);
  // A new main-frame document replaces everything: the next batch is a reset.
  m.treeChange(json(R"({"ax_tree_id":"next","updates":[{"has_tree_data":true,"tree_data":{"tree_id":"next"},"root_id":1,"nodes":[{"id":1,"role":"rootWebArea","location":{"x":0,"y":0,"width":800,"height":600},"attributes":{"name":"Next"}}]}]})"), now);
  CHECK(m.nodeCount() == 1);
  pump(m, zen, now);
  CHECK(zen.nodes.size() == 1 && zen.byName("Next"));
  // Late updates of the replaced document are ignored for a moment...
  m.treeChange(json(page(Basic)), now);
  CHECK(m.nodeCount() == 1);
}

static void testCapsAndText() {
  CHECK(axio::ax::clipText("abc", 10) == "abc");
  std::string clipped = axio::ax::clipText(std::string("\xe2\x82\xac\xe2\x82\xac\xe2\x82\xac\xe2\x82\xac"), 8);  // 4 x U+20AC
  CHECK(clipped == "\xe2\x82\xac\xe2\x80\xa6");  // one euro + ellipsis = 6 bytes <= 8
  CHECK(axio::ax::clipText("\xff\xfe", 100) == "\xef\xbf\xbd\xef\xbf\xbd");  // invalid UTF-8 -> U+FFFD
  std::string out;
  axio::ax::appendJson(out, std::string("a\"\\\n\x01\xe2\x80\xa8", 8));
  CHECK(out == "\"a\\\"\\\\\\n\\u0001\\u2028\"");
  Mirror m; m.reset();
  // 100 groups of 300 leaves: 30101 nodes offered, all reachable.
  std::string nodes = R"({"id":1,"role":"rootWebArea","location":{"x":0,"y":0,"width":800,"height":600},"child_ids":[)";
  for (int g = 2; g < 102; g++) nodes += (g > 2 ? "," : "") + std::to_string(g);
  nodes += "]}";
  for (int g = 2; g < 102; g++) {
    nodes += R"(,{"id":)" + std::to_string(g) + R"(,"role":"group","child_ids":[)";
    for (int k = 0; k < 300; k++) nodes += (k ? "," : "") + std::to_string(1000 + g * 300 + k);
    nodes += "]}";
  }
  for (int g = 2; g < 102; g++)
    for (int k = 0; k < 300; k++) nodes += R"(,{"id":)" + std::to_string(1000 + g * 300 + k) + R"(,"role":"genericContainer"})";
  m.treeChange(json(page(nodes)), 0);
  CHECK(m.nodeCount() == axio::ax::MaxNodes);
  CHECK(m.truncated());
  // Hostile role strings and urls are dropped.
  Mirror h; h.reset();
  h.treeChange(json(page(R"x({"id":1,"role":"root WebArea<script>","location":{"x":0,"y":0,"width":1,"height":1},"attributes":{"url":"javascript:alert(1)","checkedState":"<b>"}})x")), 0);
  std::string line = h.flush(Target, 800, 600, 1, 1000)[0];
  CHECK(line.find("\"role\":\"unknown\"") != std::string::npos);
  CHECK(line.find("javascript:") == std::string::npos && line.find("<b>") == std::string::npos);
}

int main() {
  testBasicTreeAndRedaction();
  testDeletionAndReparenting();
  testBoundsResolution();
  testChunkingAndContinuations();
  testFlowControlAndAcks();
  testLocationChanges();
  testChildTreesAndNavigation();
  testCapsAndText();
  if (failures) { std::fprintf(stderr, "a11y_core_test: %d failure(s)\n", failures); return 1; }
  std::puts("a11y_core_test: PASS");
  return 0;
}
