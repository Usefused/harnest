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
  const workspaceNavButtons = ["chat", "evals"].map((name) => {
    const button = new Element("button");
    button.dataset.workspace = name;
    return button;
  });
  const document = {
    getElementById: get,
    querySelector: get,
    querySelectorAll: (selector) => {
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
  const context = vm.createContext({ document, window, console, URL, URLSearchParams, crypto: require("node:crypto").webcrypto });
  return { get, context, run: (code) => vm.runInContext(code, context), window, location, history, listeners, getPushCount: () => pushCount };
}

function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }

function workspacePage() {
  const page = browser();
  const code = fs.readFileSync(path.join(assets, "playground.js"), "utf8").replace(/initialize\(\);\s*$/, "");
  vm.runInContext(fs.readFileSync(path.join(assets, "agui.js"), "utf8"), page.context);
  vm.runInContext(code, page.context);
  page.run('runtime.sessionId = "session-kept"; runtime.evalCatalog = {suites: []}; ui.token.value = "";');
  return page;
}

test("private input displays its schema and clears values before a failed submission", async () => {
  const page = workspacePage();
  const form = page.run('clientToolResultForm({id:"private", name:"credentials", privateInput:true, inputSchema:{type:"object"}}, "response", document.createElement("section"))');
  const nodes = descendants(form);
  assert.ok(nodes.some(node => node.tag === "pre" && node.textContent.includes('"object"')));
  assert.ok(nodes.some(node => node.textContent === "Private input JSON"));
  const input = nodes.find(node => node.tag === "textarea");
  assert.equal(input.autocomplete, "off");
  assert.equal(input.spellcheck, false);
  page.context.privateField = input;
  page.run('clearError=()=>{}; startAgentResume=()=>{}; finishFailedRequest=()=>{}; api=async()=>{if(privateField.value)throw new Error("private value retained");throw new Error("unavailable");}');
  input.value = '{"value":"private-secret"}';
  await page.run('submitClientToolResult({id:"private", privateInput:true}, "response", document.createElement("section"), privateField, document.createElement("button"))');
  assert.equal(input.value, "");
  input.value = "invalid private secret";
  page.run('showError=()=>{}');
  await page.run('submitClientToolResult({id:"private", privateInput:true}, "response", document.createElement("section"), privateField, document.createElement("button"))');
  assert.equal(input.value, "");
});

test("visible idle tab recovers after reload and shares the check with Send", async () => {
  const page = workspacePage();
  let generation = "first", available = true, sessionExists = true, creates = 0, lookups = 0;
  page.context.fetch = async (path, options) => {
    if (!available) throw new Error("Restarting");
    if (path === "/_harnest/generation") return {ok: true, text: async () => generation};
    if (path === "/sessions/session-kept") {
      lookups++;
      return {ok: sessionExists, status: 404, json: async () => ({detail: "Session not found"})};
    }
    if (path === "/sessions" && options.method === "POST") {
      creates++;
      return {ok: true, json: async () => ({id: "replacement"})};
    }
    if (path === "/sessions") return {ok: true, json: async () => ({sessions: [{id: "replacement"}]})};
    if (path.endsWith("/messages")) return {ok: true, json: async () => ({messages: []})};
    throw new Error("Unexpected request: " + path);
  };
  page.run(`runtime.generation = "first"; ui.input.value = "my draft";
    ui.sessionSelect.selectedOptions = []; loadTraces = async () => {};
    window.setInterval = fn => { window.tick = fn; }; monitorReload();`);
  const tick = async () => { page.window.tick(); await page.run("runtime.reloadCheck"); };
  await tick();
  assert.equal(lookups, 0);
  generation = "second";
  await tick();
  assert.equal(lookups, 1);
  assert.equal(page.run("runtime.sessionId"), "session-kept");
  available = false;
  await page.run("submitMessage({preventDefault(){}})");
  assert.equal(page.run("ui.input.value"), "my draft");
  assert.equal(creates, 0);
  available = true; sessionExists = false; generation = "third";
  page.window.tick();
  await Promise.all([page.run("checkReload()"), page.run("checkReload()")]);
  assert.equal(creates, 1);
  assert.equal(page.run("ui.sessionSelect.value"), "replacement");
  assert.equal(new URL(page.location.href).searchParams.get("session"), "replacement");
  assert.equal(page.run("ui.input.value"), "my draft");
  await tick();
  assert.equal(lookups, 2);
});

// Recovery must yield ownership when the user changes sessions or credentials during its list refresh.
for (const change of ["session", "credentials"]) test(`reload recovery preserves newer ${change} during refresh`, async () => {
  const page = workspacePage();
  let release, announce;
  // Separate request arrival from completion so the user action occurs inside the network round trip.
  const started = new Promise(resolve => { announce = resolve; });
  const refresh = new Promise(resolve => { release = resolve; });
  // Creation completes first; only the list refresh remains in flight during the ownership change.
  page.context.fetch = async (_path, options) => {
    if (options.method === "POST") return {ok: true, json: async () => ({id: "replacement"})};
    announce();
    return refresh;
  };
  page.run(`ui.sessionSelect.selectedOptions = []; ui.input.value = "my draft";
    let viewLoads = 0;
    // Stale recovery must not start loading state, messages, or traces for the abandoned selection.
    loadSessionState = loadSessionMessages = loadTraces = async () => { viewLoads++; };`);
  const recovery = page.run('replaceMissingSession("session-kept", "")');
  await started;
  // Exercise identity and credential changes independently because both invalidate the old request.
  page.run(change === "session" ? 'setActiveSession("user-selected")' : 'ui.token.value = "new-token"');
  release({ok: true, json: async () => ({sessions: [{id: "replacement"}, {id: "user-selected"}]})});
  await recovery;
  assert.equal(page.run("runtime.sessionId"), change === "session" ? "user-selected" : "replacement");
  assert.equal(page.run("ui.token.value"), change === "credentials" ? "new-token" : "");
  assert.equal(page.run("viewLoads"), 0);
  assert.equal(page.run("ui.input.value"), "my draft");
});

// Every asynchronous session view must discard a response after its owner changes.
for (const [loader, renderer] of [["loadSessionState", "renderSessionState"], ["loadSessionMessages", "renderSessionMessages"], ["loadTraces", "renderTraces"]]) test(`${loader} discards a delayed response for an abandoned selection`, async () => {
  const page = workspacePage();
  let release, announce;
  // Hold the response body so the loader has captured its original session before the user switches.
  const started = new Promise(resolve => { announce = resolve; });
  const response = new Promise(resolve => { release = resolve; });
  // The response arrives normally; decoding remains pending until the ownership change.
  page.context.fetch = async () => ({ok: true, json: () => { announce(); return response; }});
  page.run(`let viewRenders = 0;
    // Observe visible updates rather than the loader's internal guard shape.
    ${renderer} = () => { viewRenders++; };`);
  const loading = page.run(`${loader}()`);
  await started;
  page.run('setActiveSession("user-selected"); ui.token.value = "new-token";');
  release({id: "session-kept", state: {stale: true}, messages: [{role: "user", content: "stale"}], traces: [{id: "stale"}]});
  await loading;
  assert.equal(page.run("viewRenders"), 0);
  assert.equal(page.run("runtime.sessionId"), "user-selected");
});

test("switching Playground and Evals preserves conversation and active session", async () => {
  const page = workspacePage();
  const conversation = page.get("#conversation");
  conversation.children.push(new Element("message"));
  await page.run('selectWorkspace("evals")');
  assert.equal(page.get("#eval-workspace").hidden, false);
  assert.equal(page.get("#chat-workspace").hidden, true);
  await page.run('selectWorkspace("chat")');
  assert.equal(page.get("#eval-workspace").hidden, true);
  assert.equal(page.get("#chat-workspace").hidden, false);
  assert.equal(page.run("runtime.sessionId"), "session-kept");
  assert.equal(conversation.children.length, 1);
});

test("switching sessions clears prior turns and tool cards and restores a shareable URL", () => {
  const page = workspacePage();
  page.location.href = "http://127.0.0.1:28081/?session=selected-session&view=chat";
  page.run(`
    ui.conversation.append(document.createElement("article"));
    runtime.toolCards.set("old-tool", {});
    runtime.clientToolCards.set("old-client-tool", {});
    setActiveSession(sessionIdFromLocation());
    syncSessionUrl(runtime.sessionId);
  `);
  assert.equal(page.run("runtime.sessionId"), "selected-session");
  assert.equal(page.run("ui.conversation.children.length"), 1);
  assert.equal(page.run("ui.conversation.children[0] === ui.emptyState"), true);
  assert.equal(page.run("runtime.toolCards.size + runtime.clientToolCards.size"), 0);
  assert.equal(new URL(page.location.href).searchParams.get("session"), "selected-session");
});

test("a failed request marks only unfinished tool cards as failed", () => {
  const page = workspacePage();
  page.run(`
    const running = createToolCard("lookup", "running");
    const completed = createToolCard("lookup", "completed");
    completed.detail.dataset.status = "completed";
    runtime.toolCards.set("running", running);
    runtime.toolCards.set("completed", completed);
    failRunningTools();
  `);
  assert.equal(page.run('runtime.toolCards.get("running").detail.dataset.status'), "failed");
  assert.equal(page.run('runtime.toolCards.get("running").status.textContent'), "Failed");
  assert.equal(page.run('runtime.toolCards.get("completed").detail.dataset.status'), "completed");
});

test("tab navigation and browser history restore Chat and Evals", async () => {
  const page = workspacePage();
  page.run("bindEvents()");
  const nav = page.context.document.querySelectorAll(".workspace-nav-item");
  await nav[1].listeners.click();
  assert.equal(new URL(page.location.href).searchParams.get("view"), "evals");
  assert.equal(page.getPushCount(), 1);
  page.location.href = "http://127.0.0.1:28081/";
  for (const listener of page.listeners.popstate) await listener();
  assert.equal(page.get("#chat-workspace").hidden, false);
  assert.equal(page.get("#eval-workspace").hidden, true);
  page.location.href = "http://127.0.0.1:28081/?view=evals";
  for (const listener of page.listeners.popstate) await listener();
  assert.equal(page.get("#eval-workspace").hidden, false);
  assert.equal(page.getPushCount(), 1);
});

test("retired Studio bookmarks return to Chat and preserve unrelated URL state", async () => {
  const page = workspacePage();
  page.location.href = "http://127.0.0.1:28081/?view=studio&studioView=connections&session=session-kept#anchor";
  await page.run("selectWorkspace(workspaceFromLocation())");
  assert.equal(page.run("runtime.workspace"), "chat");
  assert.equal(page.get("#chat-workspace").hidden, false);
  const url = new URL(page.location.href);
  assert.equal(url.searchParams.has("view"), false);
  assert.equal(url.searchParams.has("studioView"), false);
  assert.equal(url.searchParams.get("session"), "session-kept");
  assert.equal(url.hash, "#anchor");
  await page.run('selectWorkspace("studio")');
  assert.equal(page.run("runtime.workspace"), "chat");
});

test("initial load normalizes an old Studio bookmark even when agent discovery fails", async () => {
  const page = workspacePage();
  page.location.href = "http://127.0.0.1:28081/?view=studio&studioView=tools&session=session-kept";
  // Isolate startup navigation from theme storage and unrelated transport requests.
  page.run(`
    selectTheme = () => {};
    storedTheme = () => "dark";
    bindEvents = () => {};
    resizeComposer = () => {};
    loadAgent = async () => { throw new Error("Agent unavailable"); };
    loadSessions = async () => {};
  `);
  await page.run("initialize()");
  assert.equal(page.run("runtime.workspace"), "chat");
  assert.equal(page.get("#chat-workspace").hidden, false);
  assert.equal(new URL(page.location.href).searchParams.has("view"), false);
  assert.equal(new URL(page.location.href).searchParams.has("studioView"), false);
  assert.equal(new URL(page.location.href).searchParams.get("session"), "session-kept");
});

function builderPage() {
  const page = browser();
  page.context.queueMicrotask = callback => callback();
  // Expose editor entrypoints in the VM; generation reload is covered by runtime integration.
  const source = fs.readFileSync(path.join(assets, "builder.js"), "utf8").replace(
    "return {openEvals, captureConversation, syncEvalSelection}",
    "return {editCase, captureConversation, showError, setup(request, capabilities = authoring) { api = request; authoring = capabilities; saved = async () => {}; }}"
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

test("plain serve offers a workspace save and reports when the suite can run", async () => {
  const page = builderPage(), writes = [];
  page.context.document.querySelectorAll = () => [{
    classList: {contains: role => role === "user"}, querySelector: () => ({textContent: "Hello"}),
  }];
  page.context.request = async (url, options) => {
    writes.push([url, JSON.parse(options.body)]);
    return {json: async () => ({})};
  };
  let review;
  page.context.setTimeout = callback => { review = callback; };
  await page.run("harnestBuilder.setup(request, {available:false, captureAvailable:true, suites:[]}); harnestBuilder.captureConversation()");
  const submit = () => descendants(page.get("builder-dialog")).find(node => node.tag === "form").listeners.submit({preventDefault() {}});
  await submit();
  await review();
  inputByLabel(page, "Suite ID").value = "plain";
  await submit();
  const [[url, body]] = writes;
  assert.equal(url, "/_harnest/authoring/document");
  assert.equal(body.path, "evals/plain.evalset.json");
  assert.equal(JSON.parse(body.text).eval_cases[0].conversation[0].userContent.parts[0].text, "Hello");
  assert.match(page.get("eval-authoring-status").textContent, /Saved to evals\/plain\.evalset\.json.*Restart/);
});

test("read-only chat capture downloads a reviewed eval suite without a workspace write", async () => {
  const page = builderPage();
  const message = (user, text) => ({
    classList: {contains: role => user && role === "user"},
    querySelector: () => ({textContent: text, markdownSource: user ? undefined : text}),
  });
  page.context.document.querySelectorAll = () => [
    message(true, "First question"), message(false, "First answer"),
    message(true, "Follow-up"), message(false, "Second answer"),
  ];
  let blob, filename, revoked;
  page.context.Blob = Blob;
  page.context.URL = {
    createObjectURL: value => { blob = value; return "blob:eval"; },
    revokeObjectURL: url => { revoked = url; },
  };
  page.context.setTimeout = fn => fn();
  page.context.document.createElement = tag => {
    const node = new Element(tag);
    if (tag === "a") {
      node.click = () => { filename = node.download; assert.equal(node.href, "blob:eval"); };
      node.remove = () => {};
    }
    return node;
  };
  await page.run("harnestBuilder.captureConversation()");
  const dialog = page.get("builder-dialog");
  assert.ok(descendants(dialog).some(node => node.textContent === "Download eval"));
  inputByLabel(page, "Suite ID").value = "chat_review";
  inputByLabel(page, "Expected response").value = "Reviewed answer";
  await descendants(dialog).find(node => node.tag === "form").listeners.submit({preventDefault() {}});
  assert.equal(dialog.open, false);
  assert.equal(filename, "chat_review.evalset.json");
  assert.equal(revoked, "blob:eval");
  const suite = JSON.parse(await blob.text());
  assert.equal(suite.eval_set_id, "chat_review");
  assert.deepEqual(suite.eval_cases[0].conversation.map(turn => [
    turn.userContent.parts[0].text, turn.finalResponse.parts[0].text,
  ]), [["First question", "Reviewed answer"], ["Follow-up", "Second answer"]]);
  assert.match(page.get("eval-authoring-status").textContent, /chat_review\.evalset\.json/);
});

test("evaluation errors render without the removed Studio status element", () => {
  const page = builderPage();
  page.context.document.getElementById = id => id.startsWith("studio-") ? null : page.get(id);
  page.run('harnestBuilder.showError(new Error("Evaluation save failed"))');
  assert.equal(page.get("eval-authoring-status").textContent, "Evaluation save failed");
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


test("AG-UI renders streaming tools and output without replaying a transcript", async () => {
  const page=workspacePage();
  page.run(`
    var sent=[], rendered=[];
    api=async(path,options)=>{sent.push({path,body:JSON.parse(options.body)});return {};};
    consumeSse=async(response,handle)=>[
      {type:"RUN_STARTED"}, {type:"TEXT_MESSAGE_START",messageId:"one"},
      {type:"TEXT_MESSAGE_CONTENT",delta:"Hello"}, {type:"TEXT_MESSAGE_END"},
      {type:"TOOL_CALL_START",toolCallId:"tool",toolCallName:"lookup"},
      {type:"TOOL_CALL_ARGS",toolCallId:"tool",delta:'{"id":1}'}, {type:"TOOL_CALL_END",toolCallId:"tool"},
      {type:"TOOL_CALL_RESULT",toolCallId:"tool",content:'{"ok":true}'},
      {type:"STATE_DELTA",delta:[{op:"add",path:"/__proto__/bad",value:true}]},
      {type:"RUN_FINISHED",result:{ok:true}}
    ].forEach(handle);
    beginStreamingOutput=()=>{}; beginToolBoundary=()=>{}; clearTypingIndicator=()=>{};
    appendStreamingText=value=>rendered.push(value);
    appendToolCall=(...value)=>rendered.push(value); appendToolResult=(...value)=>rendered.push(value);
    appendResult=value=>rendered.push(value); finishRequest=async()=>{};
  `);
  await page.run('harnestAgui.run("session-kept","Hello")');
  const sent=JSON.parse(page.run('JSON.stringify(sent[0])'));
  assert.equal(sent.path,"/agui"); assert.equal(sent.body.threadId,"session-kept");
  assert.equal(sent.body.messages.length,1); assert.equal(sent.body.messages[0].role,"user");
  assert.deepEqual(sent.body.state,{});
  assert.deepEqual(JSON.parse(page.run('JSON.stringify(rendered)')),["Hello",["lookup",{id:1},"tool"],["lookup",{ok:true},"tool"],{ok:true}]);
  assert.equal(page.run('({}).bad'),undefined);
  page.run('consumeSse=async()=>{};');
  await assert.rejects(page.run('harnestAgui.run("session-kept","next")'),/ended before the run finished/);
});

test("AG-UI batches approvals and private input only in resume and clears rejected secrets", async () => {
  const page=workspacePage();
  page.run(`
    var sent=[];
    api=async(path,options)=>{sent.push(JSON.parse(options.body));return {};};
    consumeSse=async(response,handle)=>handle({type:"RUN_FINISHED",outcome:{interrupts:[
      {metadata:{harnest:{id:"approval",type:"human_approval",message:"Send?"}}},
      {metadata:{harnest:{id:"private",type:"client_tool",name:"secret",privateInput:true,inputSchema:{type:"object"}}}}
    ]}});
    clearTypingIndicator=()=>{}; scrollConversation=()=>{}; startAgentResume=()=>{};
    finishRequest=async()=>{}; finishFailedRequest=()=>{}; setStatus=()=>{};
  `);
  await page.run('harnestAgui.run("session-kept","start")');
  assert.equal(page.run('harnestAgui.pending("session-kept")'),true);
  const form=page.get("#conversation").children.at(-1), fields=descendants(form);
  const approval=fields.find(node=>node.tag==="select"), secret=fields.find(node=>node.tag==="textarea");
  assert.equal(secret.autocomplete,"off"); approval.value="approve"; secret.value='{"value":"sensitive"}';
  page.context.secretField=secret;
  page.run('api=async(path,options)=>{if(secretField.value)throw new Error("secret retained");sent.push(JSON.parse(options.body));throw new Error("rejected");};');
  await form.listeners.submit({preventDefault(){}});
  assert.equal(secret.value,"");
  const body=JSON.parse(page.run('JSON.stringify(sent.at(-1))'));
  assert.deepEqual(body.messages,[]);assert.deepEqual(body.state,{});
  assert.deepEqual(body.resume,[{interruptId:"approval",status:"resolved",payload:{approved:true}},{interruptId:"private",status:"resolved",payload:{value:"sensitive"}}]);
  secret.value="invalid secret";
  await form.listeners.submit({preventDefault(){}});
  assert.equal(secret.value,""); assert.equal(page.run('sent.length'),2);
  assert.throws(()=>page.run('setActiveSession("another")'),/Finish or cancel/);
  page.run('runtime.busy=true;');
  const transport=new Element("button");transport.dataset.transport="agui";page.context.transport=transport;
  page.run('selectTransport(transport)');assert.equal(page.run('runtime.transport'),"stream");
});


test("custom UI event payloads render as inert JSON across native and AG-UI transports",async()=>{
  const page=workspacePage();
  page.run('scrollConversation=()=>{}; renderOutputItem({type:"ui_event",name:"app.card",value:{html:"<script>bad()</script>"}});');
  let card=page.get("#conversation").children.at(-1);
  assert.equal(card.children[0].textContent,"UI event · app.card");
  assert.ok(card.children[1].textContent.includes("<script>bad()</script>"));
  assert.equal(descendants(card).some(node=>node.tag==="script"),false);
  page.run(`api=async()=>({}); consumeSse=async(response,handle)=>[{type:"CUSTOM",name:"app.status",value:{percent:50}},{type:"RUN_FINISHED"}].forEach(handle); clearTypingIndicator=()=>{}; finishRequest=async()=>{};`);
  await page.run('harnestAgui.run("session-kept","start")');
  card=page.get("#conversation").children.at(-1);
  assert.equal(card.children[0].textContent,"UI event · app.status");
});
