"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const assets = path.resolve(__dirname, "../../src/harnest/_playground");

class Element {
  constructor(tag = "div") {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.dataset = {};
    this.value = "";
    this.textContent = "";
    this.hidden = false;
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  append(...nodes) { for (const node of nodes) node.parentElement = this; this.children.push(...nodes); }
  insertBefore(node, before) { this.children = this.children.filter(item => item !== node); this.children.splice(this.children.indexOf(before), 0, node); }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() {}
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelector() { return this.children[0] ||= new Element("ul"); }
  querySelectorAll() { return this.children.flatMap((node) => [node, ...node.querySelectorAll()]); }
}

function browser() {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const studioViewButtons = ["architecture", "tools", "connections", "configuration"].map((name) => {
    const button = new Element("button");
    button.dataset.studioView = name;
    return button;
  });
  const workspaceNavButtons = ["chat", "studio", "evals"].map((name) => {
    const button = new Element("button");
    button.dataset.workspace = name;
    return button;
  });
  const document = {
    getElementById: get,
    querySelector: get,
    querySelectorAll: (selector) => {
      if (selector === "[data-studio-view]") return studioViewButtons;
      if (selector === ".workspace-nav-item") return workspaceNavButtons;
      return [];
    },
    addEventListener() {},
    createElement: (tag) => new Element(tag),
    createElementNS: (_, tag) => new Element(tag),
    body: new Element("body"),
  };
  const listeners = {};
  const location = { href: "http://127.0.0.1:28081/" };
  Object.defineProperty(location, "search", { get() { return new URL(this.href).search; } });
  let pushCount = 0;
  const history = {
    state: null,
    pushState(state, _title, url) { this.state = state; pushCount += 1; location.href = String(url); },
    replaceState(state, _title, url) { this.state = state; location.href = String(url); },
  };
  const window = {
    location,
    history,
    addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
  };
  const context = vm.createContext({ document, window, console, URL, URLSearchParams });
  vm.runInContext(fs.readFileSync(path.join(assets, "studio.js"), "utf8"), context);
  return { get, context, run: (code) => vm.runInContext(code, context), window, location, history, listeners, getPushCount: () => pushCount };
}

function snapshot(sourceAvailable = false) {
  return {
    source_digest: "abcdef0123456789", source_available: sourceAvailable,
    blocks: [
      { id: "agent", kind: "agent", name: "<script>unsafe</script>", path: "agent.py", line: 1, config: { model: "test" } },
      { id: "tool", kind: "tool", name: "lookup", path: "tools/lookup.py", line: 2, config: {} },
    ],
    connections: [{ source: "agent", target: "tool", kind: "capability" }],
    files: [{ path: "agent.py", language: "python", size: 100 }], diagnostics: [],
  };
}

function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }

test("architecture renders real components as text and distinguishes capabilities from workflow", async () => {
  const page = browser();
  page.context.api = async () => ({ json: async () => snapshot() });
  await page.run("harnestStudio.open(api)");
  const map = descendants(page.get("studio-canvas"));
  assert.ok(map.some((node) => node.textContent === "<script>unsafe</script>"));
  assert.ok(!map.some((node) => node.tag === "script"));
  const edge = map.find((node) => node.attributes.class === "studio-edge capability");
  assert.ok(edge);
  assert.equal(edge.attributes["marker-end"], undefined);
  const detail = descendants(page.get("studio-detail"));
  assert.ok(detail.some((node) => node.textContent.includes("Uses capability")));
  assert.ok(!detail.some((node) => node.textContent === "View source"));
  const tool = map.find((node) => node.dataset.blockId === "tool");
  tool.listeners.keydown({ key: "Enter", preventDefault() {} });
  assert.equal(page.get("studio-detail").children[1].textContent, "lookup");
});

test("local source is loaded on demand and stale source cannot replace another selection", async () => {
  const page = browser();
  const calls = [];
  let release;
  page.context.api = async (url) => {
    calls.push(url);
    if (url.includes("/source")) return new Promise((resolve) => { release = resolve; });
    return { json: async () => snapshot(true) };
  };
  await page.run("harnestStudio.open(api)");
  assert.equal(calls.length, 1);
  const button = descendants(page.get("studio-detail")).find((node) => node.textContent === "View source");
  const pending = button.listeners.click();
  assert.equal(calls[1], "/_harnest/studio/source?path=agent.py");
  const tool = descendants(page.get("studio-canvas")).find((node) => node.dataset.blockId === "tool");
  tool.listeners.click();
  release({ json: async () => ({ text: "<script>source</script>" }) });
  await pending;
  assert.equal(page.get("studio-detail").children[1].textContent, "lookup");
  assert.ok(!descendants(page.get("studio-detail")).some((node) => node.textContent.includes("<script>source")));
});

test("failed reload clears old build information and supports retry", async () => {
  const page = browser();
  let fail = false;
  page.context.api = async () => {
    if (fail) throw new Error("Authentication required");
    return { json: async () => snapshot() };
  };
  await page.run("harnestStudio.open(api)");
  fail = true;
  await page.get("studio-refresh").listeners.click();
  assert.equal(page.get("studio-status").textContent, "Authentication required");
  assert.equal(page.get("studio-canvas").children.length, 0);
  assert.equal(page.get("studio-detail").children.length, 0);
  fail = false;
  await page.run("harnestStudio.open(api)");
  assert.ok(page.get("studio-canvas").children.length);
});

test("switching Studio and Playground preserves conversation and active session", async () => {
  const page = browser();
  let code = fs.readFileSync(path.join(assets, "playground.js"), "utf8");
  code = code.replace(/initialize\(\);\s*$/, "");
  vm.runInContext(code, page.context);
  page.context.fetch = async () => ({ ok: true, json: async () => snapshot() });
  page.run('runtime.sessionId = "session-kept"; ui.token.value = "";');
  const conversation = page.get("#conversation");
  conversation.children.push(new Element("message"));
  await page.run('selectWorkspace("studio")');
  assert.equal(page.get("#studio-workspace").hidden, false);
  assert.equal(page.get("#chat-workspace").hidden, true);
  await page.run('selectWorkspace("chat")');
  assert.equal(page.get("#studio-workspace").hidden, true);
  assert.equal(page.get("#chat-workspace").hidden, false);
  assert.equal(page.run("runtime.sessionId"), "session-kept");
  assert.equal(conversation.children.length, 1);
});


test("tab navigation records URLs so back/forward returns to the previous view", async () => {
  const page = browser();
  let code = fs.readFileSync(path.join(assets, "playground.js"), "utf8");
  code = code.replace(/initialize\(\);\s*$/, "");
  vm.runInContext(code, page.context);
  page.context.fetch = async () => ({ ok: true, json: async () => snapshot() });
  page.run("bindEvents()");

  // Workspace tab clicks push a history entry and update the URL.
  const nav = page.context.document.querySelectorAll(".workspace-nav-item");
  await nav[1].listeners.click();
  assert.equal(new URL(page.location.href).searchParams.get("view"), "studio");
  assert.equal(page.getPushCount(), 1);

  // A Studio view tab records the nested view alongside the workspace.
  const views = page.context.document.querySelectorAll("[data-studio-view]");
  views[2].listeners.click();
  assert.equal(new URL(page.location.href).searchParams.get("view"), "studio");
  assert.equal(new URL(page.location.href).searchParams.get("studioView"), "connections");
  assert.equal(page.getPushCount(), 2);

  // Back restores the previous Studio view without leaving the workspace.
  page.location.href = "http://127.0.0.1:28081/?view=studio";
  for (const listener of page.listeners.popstate) listener();
  assert.equal(new URL(page.location.href).searchParams.get("view"), "studio");
  assert.equal(new URL(page.location.href).searchParams.has("studioView"), false);

  // Back again restores the previous workspace.
  page.location.href = "http://127.0.0.1:28081/";
  for (const listener of page.listeners.popstate) listener();
  assert.equal(page.get("#studio-workspace").hidden, true);
  assert.equal(page.get("#chat-workspace").hidden, false);
  assert.equal(new URL(page.location.href).searchParams.has("view"), false);
});


test("selecting a graph shows its owned workflow edges and opens the target component", async () => {
  const page = browser();
  const data = snapshot();
  data.blocks.unshift({ id: "graph", kind: "graph", name: "root", path: "agent.py", line: 1, config: {} });
  data.connections = [{ graph: "graph", source: "agent", target: "tool", kind: "workflow", route: "ready" }];
  page.context.api = async () => ({ json: async () => data });
  await page.run("harnestStudio.open(api)");
  const detail = descendants(page.get("studio-detail"));
  assert.ok(!detail.some((node) => node.textContent === "No statically resolved connections."));
  const edge = detail.find((node) => node.className === "studio-relation");
  assert.match(edge.textContent, /unsafe.*Workflow.*ready.*lookup/);
  edge.listeners.click();
  assert.equal(page.get("studio-detail").children[1].textContent, "lookup");
});


function builderPage() {
  const page = browser();
  page.context.queueMicrotask = callback => callback();
  // Expose editor entrypoints in the VM; generation reload is covered by runtime integration.
  const source = fs.readFileSync(path.join(assets, "builder.js"), "utf8").replace(
    "return {openStudio, renderStudio, appendEdit, openEvals, captureConversation, syncEvalSelection}",
    "return {openStudio, editCase, selectTools, setup(request) { api = request; saved = async () => {}; }}"
  );
  vm.runInContext(source, page.context);
  return page;
}

function inputByLabel(page, label) {
  return descendants(page.get("builder-dialog")).find(node => node.tag === "label" && node.textContent === label).children[0];
}

test("case form separates sections and preserves native fields across failed-save retry", async () => {
  const page = builderPage(), writes = [];
  const original = {eval_set_id: "quality", name: "Quality", eval_cases: [{evalId: "first", conversation: [{userContent: {role: "user", parts: [{text: "original"}, {inlineData: {mimeType: "image/png", data: "abc"}}]}, finalResponse: {role: "model", parts: [{text: "expected"}]}, intermediateData: {toolUses: [{name: "lookup", args: {query: "original"}}]}}]}]};
  page.context.documentValue = {path: "evals/quality.evalset.json", revision: "reviewed", text: JSON.stringify(original)};
  page.context.api = async (_, options) => {
    if (options.method === "PUT") { writes.push(JSON.parse(options.body)); if (writes.length === 1) throw new Error("temporary failure"); }
    return {json: async () => page.context.documentValue};
  };
  await page.run("harnestBuilder.setup(api); harnestBuilder.editCase(documentValue, 0, true)");
  const legends = descendants(page.get("builder-dialog")).filter(node => node.tag === "legend").map(node => node.textContent);
  assert.deepEqual(legends, ["Case details", "Input & expected behavior"]);
  inputByLabel(page, "Case ID").value = "copy";
  inputByLabel(page, "Input prompt").value = "changed";
  const form = descendants(page.get("builder-dialog")).find(node => node.tag === "form");
  await form.listeners.submit({preventDefault() {}});
  assert.equal(page.get("builder-dialog").open, true);
  await form.listeners.submit({preventDefault() {}});
  assert.equal(page.get("builder-dialog").open, false);
  for (const write of writes) {
    const suite = JSON.parse(write.text);
    assert.equal(suite.eval_cases.length, 2);
    assert.equal(write.revision, "reviewed");
    assert.deepEqual(suite.eval_cases[0], original.eval_cases[0]);
    const copy = suite.eval_cases[1];
    assert.equal(copy.evalId, "copy");
    assert.equal(copy.conversation[0].userContent.parts[0].text, "changed");
    assert.deepEqual(copy.conversation[0].userContent.parts[1], original.eval_cases[0].conversation[0].userContent.parts[1]);
    assert.deepEqual(copy.conversation[0].intermediateData, original.eval_cases[0].conversation[0].intermediateData);
  }
});

test("remote read-only authoring denial does not hide the architecture", async () => {
  const page = builderPage();
  page.context.data = snapshot();
  page.context.api = async () => { throw new Error("Local editing only"); };
  await page.run("harnestBuilder.openStudio(api, data, 'agent', () => {})");
  assert.equal(page.get("studio-access").textContent, "Read only");
  assert.equal(page.get("add-mcp").disabled, true);
  assert.ok(descendants(page.get("studio-configuration")).some(node => node.textContent === "<script>unsafe</script>"));
});

test("Fused connector actions stay visible but disabled until authoring can write, then unlock with a scope", async () => {
  const page = builderPage();
  page.context.data = snapshot();
  // fused-cli is installed (connectors available) but Studio was not started with --reload,
  // so authoring cannot write a client file yet. The Fused actions must stay visible (the
  // marketplace exists) but disabled, otherwise a click could run `fused-cli init --mcp` and
  // then fail to save the resulting connection, leaving an orphaned deployed server.
  page.context.api = async (url) => {
    if (url === "/_harnest/connectors") return {json: async () => ({available: true})};
    return {json: async () => ({available: false, scopes: [], suites: [], files: [], connectionScopes: []})};
  };
  await page.run("harnestBuilder.openStudio(api, data, 'agent', () => {})");
  assert.equal(page.get("browse-existing-connector").hidden, false);
  assert.equal(page.get("create-connector").hidden, false);
  assert.equal(page.get("browse-existing-connector").disabled, true);
  assert.equal(page.get("create-connector").disabled, true);
  assert.ok(descendants(page.get("studio-connections")).some(node => node.textContent.includes("harnest serve --reload")));

  // Re-open once authoring is writable and an agent scope exists: every connection action,
  // manual and Fused-discovered alike, unlocks together.
  page.context.api = async (url) => {
    if (url === "/_harnest/connectors") return {json: async () => ({available: true})};
    return {json: async () => ({available: true, scopes: [], suites: [], files: [], connectionScopes: ["."]})};
  };
  await page.run("harnestBuilder.openStudio(api, data, 'agent', () => {})");
  assert.equal(page.get("add-mcp").disabled, false);
  assert.equal(page.get("browse-existing-connector").disabled, false);
  assert.equal(page.get("create-connector").disabled, false);
});

test("Fused connector actions stay hidden when fused-cli is not installed, regardless of authoring state", async () => {
  const page = builderPage();
  page.context.data = snapshot();
  page.context.api = async (url) => {
    if (url === "/_harnest/connectors") return {json: async () => ({available: false})};
    return {json: async () => ({available: true, scopes: [], suites: [], files: [], connectionScopes: ["."]})};
  };
  await page.run("harnestBuilder.openStudio(api, data, 'agent', () => {})");
  assert.equal(page.get("browse-existing-connector").hidden, true);
  assert.equal(page.get("create-connector").hidden, true);
});

test("MCP tool selection preserves existing names omitted from a paginated catalogue", async () => {
  const page = builderPage(), writes = [];
  page.context.block = {name: "catalog", path: "mcp/catalog.py", line: 4};
  page.context.api = async (url, options) => {
    if (options.method === "PUT") writes.push(JSON.parse(options.body));
    const value = url.includes("/query") ? {tools: [{name: "first", description: "First tool"}], nextCursor: "next"} : {revision: "checked", fields: {tools: ["other_page"]}};
    return {json: async () => value};
  };
  await page.run("harnestBuilder.setup(api); harnestBuilder.selectTools(block)");
  const form = descendants(page.get("builder-dialog")).find(node => node.tag === "form");
  await form.listeners.submit({preventDefault() {}});
  assert.deepEqual(writes[0].fields.tools, ["other_page"]);
  assert.equal(writes[0].revision, "checked");
});


test("themed selects skip disabled choices and support typeahead", () => {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(assets, "selects.js"), "utf8"), context);
  context.items = [{text: "Alpha"}, {text: "Beta", disabled: true}, {text: "Gamma"}];
  assert.equal(vm.runInContext("harnestSelects.nextIndex(items, 0, 1)", context), 2);
  assert.equal(vm.runInContext("harnestSelects.nextIndex(items, 2, -1)", context), 0);
  assert.equal(vm.runInContext("harnestSelects.nextIndex(items, 2, 1)", context), 2);
  assert.equal(vm.runInContext("harnestSelects.matchingIndex(items, 0, 'ga')", context), 2);
  assert.equal(vm.runInContext("harnestSelects.matchingIndex(items, 0, 'be')", context), 0);
});

test("themed option menus fit mobile edges and open upwards near the bottom", () => {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(assets, "selects.js"), "utf8"), context);
  const bounds = vm.runInContext("harnestSelects.menuBounds({left: 250, top: 640, bottom: 680, width: 260}, {width: 320, height: 740}, 20)", context);
  assert.ok(bounds.left >= 8);
  assert.ok(bounds.left + bounds.width <= 312);
  assert.ok(bounds.top >= 8 && bounds.top < 640);
  assert.ok(bounds.height <= 280);
});
