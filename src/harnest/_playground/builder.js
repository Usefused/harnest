/* Local evaluation authoring UI. Source and provider values are always rendered as text. */
const harnestBuilder = (() => {
  let api, authoring = {available: false, scopes: [], suites: []}, projection;
  let buildRequest = 0;
  let evalBound = false, selectedSuite = "", evalCatalog;
  const byId = id => document.getElementById(id);
  const pretty = value => JSON.stringify(value, null, 2);
  const clone = value => JSON.parse(JSON.stringify(value));
  const scoped = (scope, path) => scope === "." ? path : `${scope}/${path}`;
  const caseId = () => `case_${Date.now().toString(36)}`;

  /** Keep authored values inert throughout every editor and catalogue. */
  function el(tag, text = "", cls = "") {
    const node = document.createElement(tag);
    node.textContent = text;
    node.className = cls;
    return node;
  }
  function button(text, action, primary = false) {
    const node = el("button", text, primary ? "primary-button" : "quiet-button");
    node.type = "button";
    node.addEventListener("click", () => Promise.resolve().then(action).catch(showError));
    return node;
  }
  function field(form, label, value = "", kind = "input") {
    const wrapper = el("label", label);
    const input = el(kind);
    input.value = value;
    wrapper.append(input);
    form.append(wrapper);
    return input;
  }
  function section(form, title, description = "") {
    const group = el("fieldset", "", "builder-fieldset");
    group.append(el("legend", title));
    if (description) group.append(el("p", description, "builder-note"));
    form.append(group); return group;
  }
  function choices(form, label, values, selected) {
    const input = field(form, label, "", "select");
    for (const [value, text] of values) { const option = el("option", text); option.value = value; input.append(option); }
    if (selected !== undefined) input.value = selected;
    return input;
  }
  async function json(url, method = "GET", body) {
    const response = await api(url, {method, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    return response.json();
  }
  function showError(error) {
    const dialog = byId("builder-dialog");
    const target = dialog.open ? dialog.querySelector(".builder-error") : byId("eval-authoring-status");
    if (target) target.textContent = error.message || String(error);
  }
  /** Native dialogs provide focus trapping and Escape without global keyboard handlers. */
  function modal(title, saveLabel, save) {
    const dialog = byId("builder-dialog");
    dialog.replaceChildren();
    const heading = el("h2", title); heading.id = "builder-dialog-title";
    const form = el("form", "", "builder-form");
    const content = el("div", "", "builder-form");
    const error = el("p", "", "builder-error"); error.setAttribute("role", "alert");
    const actions = el("div", "", "builder-actions");
    const submit = button(saveLabel, () => {}, true); submit.type = "submit";
    actions.append(button("Cancel", () => dialog.close()), submit);
    form.append(content, error, actions); dialog.append(heading, form);
    form.addEventListener("submit", async event => {
      event.preventDefault(); submit.disabled = true; error.textContent = "";
      try { await save(); dialog.close(); }
      catch (failure) { error.textContent = failure.message; }
      finally { submit.disabled = false; }
    });
    if (!dialog.open) dialog.showModal();
    queueMicrotask(() => content.querySelector("input, textarea, .harnest-select-trigger, select:not(.harnest-select-native)")?.focus());
    return content;
  }
  async function refreshAuthoring() {
    try { authoring = await json("/_harnest/authoring"); }
    catch (_) { authoring = {available: false, scopes: [], suites: [], files: []}; }
    return authoring;
  }
  /** Report persistence separately from build activation, which belongs to the supervisor. */
  async function saved() {
    const message = "Saved to workspace. Waiting for the validated build to reload…";
    byId("eval-authoring-status").textContent = message;
    await refreshAuthoring().catch(() => {});
    renderSuites();
    waitForBuild(projection?.source_digest);
  }
  async function waitForBuild(previous) {
    const ticket = ++buildRequest;
    for (let attempt = 0; attempt < 45; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      if (ticket !== buildRequest) return;
      try {
        const next = await json("/_harnest/studio");
        if (!previous || next.source_digest !== previous) {
          projection = next;
          byId("eval-authoring-status").textContent = "Saved and reloaded. The updated suite is ready to run.";
          if (typeof loadEvals === "function") await loadEvals();
          return;
        }
      } catch (_) { /* The local server briefly disconnects during generation replacement. */ }
    }
    const note = "Source saved; a new build is not active yet. Check the development terminal for compilation errors, then refresh.";
    byId("eval-authoring-status").textContent = note;
  }
  async function editSource(path) {
    const document = await json(`/_harnest/authoring/document?path=${encodeURIComponent(path)}`);
    let text;
    const form = modal(`Edit ${path}`, "Save source", async () => {
      await json("/_harnest/authoring/document", "PUT", {...document, text: text.value});
      await saved();
    });
    form.append(el("p", "Changes are saved to your agent workspace and validated by the development compiler.", "builder-note"));
    text = field(form, "Source", document.text, "textarea"); text.classList.add("builder-code"); text.spellcheck = false;
  }
  async function openEvals(request, catalog) {
    api = request; evalCatalog = catalog;
    await refreshAuthoring();
    if (!projection) projection = await json("/_harnest/studio").catch(() => null);
    if (!evalBound) {
      byId("new-eval-suite").addEventListener("click", () => editCase(null));
      evalBound = true;
    }
    byId("new-eval-suite").disabled = !authoring.available;
    byId("eval-authoring-status").textContent = authoring.available ? "Create and edit cases here, or save a Playground conversation as an eval." : "Run existing suites here. Start with harnest serve --reload to create and edit evals.";
    renderSuites();
  }
  /** Share one suite selection between the editor and the existing runtime runner. */
  function syncEvalSelection() {
    const input = byId("eval-suite");
    input.parentElement.hidden = authoring.available;
    byId("eval-runner").classList.toggle("builder-runner", authoring.available);
    const document = authoring.suites.find(item => item.path === selectedSuite);
    if (authoring.available && document) {
      const id = parsedSuite(document).eval_set_id;
      input.value = document.scope === "." ? id : "";
      byId("run-eval").disabled = !input.value;
    }
  }
  function parsedSuite(document) { return JSON.parse(document.text); }
  function casesOf(suite) { return suite.eval_cases || suite.evalCases || []; }
  function identity(item) { return item.eval_id || item.evalId; }
  function textOf(content) { return (content?.parts || []).filter(part => typeof part.text === "string").map(part => part.text).join(""); }
  /** Present one selected suite, one primary run action, and compact case rows. */
  function renderSuites() {
    const host = byId("eval-authoring");
    const runner = byId("eval-runner");
    if (runner && host.contains(runner)) byId("eval-content").prepend(runner);
    host.replaceChildren();
    if (!authoring.available || !authoring.suites.length) return;
    byId("eval-empty").hidden = true;
    const workspace = el("section", "", "suite-workspace");
    const heading = el("div", "", "suite-heading");
    const picker = choices(heading, "Suite", authoring.suites.map(item => { const suite = parsedSuite(item); return [item.path, suite.name || suite.eval_set_id]; }), selectedSuite);
    if (!picker.value) picker.value = authoring.suites[0].path;
    runner.classList.add("suite-runner"); heading.append(runner); runner.hidden = false;
    workspace.append(el("h3", "Suite & run configuration", "suite-section-title"), heading); host.append(workspace);
    const content = el("div"); workspace.append(content);
    const render = () => {
      selectedSuite = picker.value; syncEvalSelection(); content.replaceChildren();
      const document = authoring.suites.find(item => item.path === selectedSuite);
      if (!document) return;
      const suite = parsedSuite(document), cases = casesOf(suite);
      const tabs = el("nav", "", "builder-tabs suite-tabs"); tabs.setAttribute("aria-label", "Suite views");
      const casePanel = el("div", "", "suite-panel"), settings = el("div", "", "suite-panel"); settings.hidden = true;
      const caseTab = button(`Cases (${cases.length})`, () => choosePanel(false)); caseTab.className = "builder-tab";
      const settingsTab = button("Settings", () => choosePanel(true)); settingsTab.className = "builder-tab";
      const choosePanel = showSettings => {
        casePanel.hidden = showSettings; settings.hidden = !showSettings;
        caseTab.setAttribute("aria-pressed", String(!showSettings)); settingsTab.setAttribute("aria-pressed", String(showSettings));
      };
      choosePanel(false); tabs.append(caseTab, settingsTab); content.append(tabs, casePanel, settings);
      const toolbar = el("div", "", "suite-case-heading");
      const caseHeading = el("div"); caseHeading.append(el("h3", "Test cases", "builder-section-title"), el("p", "Open a case to edit its prompt and expected behavior.", "builder-note")); toolbar.append(caseHeading);
      const add = button("+ Add case", () => editCase(document)); add.className = "secondary-button compact-button"; toolbar.append(add); casePanel.append(toolbar);
      const rows = el("div", "", "suite-cases");
      cases.forEach((item, index) => {
        const row = el("div", "", "suite-case-row");
        const open = button("", () => editCase(document, index)); open.className = "suite-case-open";
        open.append(el("strong", item.name || identity(item)), el("span", textOf(item.conversation?.[0]?.user_content || item.conversation?.[0]?.userContent) || "No text prompt"));
        row.append(open, actionMenu(`Actions for ${item.name || identity(item)}`, [["Duplicate", () => editCase(document, index, true)], ["Remove", () => removeCase(document, index)]]));
        rows.append(row);
      });
      if (!cases.length) rows.append(el("p", "No cases yet. Add your first check to this suite.", "builder-note"));
      casePanel.append(rows);
      settings.append(settingRow("Metrics and thresholds", "Choose how responses and tool calls are scored. Applies to suites in this agent scope.", "Configure metrics", () => editMetrics(document.scope)));
      settings.append(settingRow("Suite source", "Edit the native eval format, including advanced fields.", "Open JSON", () => editSource(document.path)));
    };
    picker.addEventListener("change", render); render();
  }
  function settingRow(title, description, action, callback) {
    const row = el("div", "", "suite-setting-row"), copy = el("div");
    copy.append(el("strong", title), el("p", description, "builder-note")); row.append(copy, button(action, callback)); return row;
  }
  /** Keep infrequent and destructive actions out of the main editing path. */
  function actionMenu(label, actions) {
    const menu = el("details", "", "builder-menu");
    const summary = el("summary", "⋯"); summary.setAttribute("aria-label", label);
    const list = el("div", "", "builder-menu-items");
    for (const [name, callback] of actions) {
      const item = button(name, () => { menu.open = false; return callback(); });
      if (name === "Remove") item.classList.add("danger-action");
      list.append(item);
    }
    menu.append(summary, list); return menu;
  }
  /** Preserve all existing case fields and multimodal content unless explicitly edited. */
  async function editCase(document, index = null, duplicate = false, captured = null) {
    if (document) document = {...document, ...await json(`/_harnest/authoring/document?path=${encodeURIComponent(document.path)}`)};
    const suite = document ? parsedSuite(document) : {eval_set_id: "", name: "", eval_cases: []};
    const existing = index === null ? null : clone(casesOf(suite)[index]);
    const item = existing || {evalId: caseId(), conversation: captured || [{userContent: {role: "user", parts: [{text: ""}]}, finalResponse: {role: "model", parts: [{text: ""}]}}]};
    if (duplicate) { delete item.eval_id; item.evalId = caseId(); }
    let suiteID, suiteName, id, title;
    const turns = [];
    const form = modal(document ? (existing && !duplicate ? "Edit eval case" : "Add eval case") : "Create eval suite", "Save eval", async () => {
      const caseKey = "eval_id" in item ? "eval_id" : "evalId";
      item[caseKey] = id.value.trim(); item.name = title.value.trim();
      for (const turn of turns) turn.apply();
      if (!item[caseKey]) throw new Error("Enter a case ID");
      const output = clone(suite);
      const cases = casesOf(output), key = "evalCases" in output ? "evalCases" : "eval_cases";
      if (index !== null && !duplicate) cases[index] = item; else cases.push(item);
      output[key] = cases;
      if (!document) { output.eval_set_id = suiteID.value.trim(); output.name = suiteName.value.trim() || output.eval_set_id; }
      const path = document?.path || `evals/${output.eval_set_id}.evalset.json`;
      await json("/_harnest/authoring/document", "PUT", {path, revision: document?.revision || "", text: pretty(output) + "\n"});
      selectedSuite = path; await saved();
    });
    if (!document) {
      const suiteDetails = section(form, "Suite details", "Group related checks into a reusable suite.");
      suiteID = field(suiteDetails, "Suite ID", "quality"); suiteID.required = true; suiteID.pattern = "[A-Za-z][A-Za-z0-9_]*";
      suiteName = field(suiteDetails, "Suite name", "Quality checks");
    }
    const caseDetails = section(form, "Case details");
    title = field(caseDetails, "Case name", item.name || "");
    id = field(caseDetails, "Case ID", identity(item)); id.required = true;
    item.conversation.forEach((turn, position) => {
      const turnGroup = section(form, item.conversation.length > 1 ? `Turn ${position + 1}` : "Input & expected behavior");
      const userKey = "user_content" in turn ? "user_content" : "userContent", replyKey = "final_response" in turn ? "final_response" : "finalResponse";
      const prompt = field(turnGroup, "Input prompt", textOf(turn[userKey]), "textarea"); prompt.required = true;
      const response = field(turnGroup, "Expected response", textOf(turn[replyKey]), "textarea");
      const details = el("details"); details.append(el("summary", "Expected tools and advanced turn data"));
      const raw = field(details, "Turn JSON (preserves tool calls and additional fields)", pretty(turn), "textarea");
      turnGroup.append(details);
      const dataKey = "intermediate_data" in turn ? "intermediate_data" : "intermediateData";
      const callsKey = "tool_uses" in (turn[dataKey] || {}) ? "tool_uses" : "toolUses";
      const expectedTools = field(details, "Expected tool calls (JSON array, optional)", pretty(turn[dataKey]?.[callsKey] || []), "textarea");
      expectedTools.rows = 2;
      details.insertBefore(expectedTools.parentElement, raw.parentElement);
      turns.push({apply() { const next = JSON.parse(raw.value); next[userKey] = replaceText(next[userKey], prompt.value, "user"); next[replyKey] = replaceText(next[replyKey], response.value, "model"); const calls = JSON.parse(expectedTools.value || "[]");
        if (!Array.isArray(calls)) throw new Error("Expected tool calls must be a JSON array");
        if (calls.length || next[dataKey]) next[dataKey] = {...next[dataKey], [callsKey]: calls};
        Object.assign(turn, next); }});
    });
  }
  function replaceText(content, text, role) {
    return {...(content || {role}), parts: [{text}, ...(content?.parts || []).filter(part => typeof part.text !== "string")]};
  }
  async function removeCase(document, index) {
    const suite = parsedSuite(document), item = casesOf(suite)[index];
    const form = modal(`Remove ${item.name || identity(item)}?`, "Remove case", async () => {
      casesOf(suite).splice(index, 1);
      await json("/_harnest/authoring/document", "PUT", {path: document.path, revision: document.revision, text: pretty(suite) + "\n"}); await saved();
    });
    form.append(el("p", "Remove this case from the suite's source file.", "builder-note"));
  }
  async function editMetrics(scope) {
    const path = scoped(scope, "evals/test_config.json");
    const document = await json(`/_harnest/authoring/document?path=${encodeURIComponent(path)}`);
    const config = document.text ? JSON.parse(document.text) : {criteria: {response_match_score: 0.8}};
    let input;
    const form = modal("Metrics & thresholds", "Save metrics", async () => {
      config.criteria = JSON.parse(input.value);
      await json("/_harnest/authoring/document", "PUT", {path, revision: document.revision, text: pretty(config) + "\n"}); await saved();
    });
    form.append(el("p", "These criteria apply to all suites in this agent scope. Existing judge settings and custom metrics are preserved.", "builder-note"));
    const criteria = section(form, "Scoring criteria", "Set each metric’s threshold. Complex metric configuration is preserved as JSON.");
    input = field(criteria, "Criteria and thresholds (JSON)", pretty(config.criteria || {}), "textarea");
    const library = section(form, "Metric library");
    const metric = choices(library, "Add a supported metric", (evalCatalog?.supportedMetrics || []).map(name => [name, name]));
    library.append(button("Add metric", () => { const current = JSON.parse(input.value); current[metric.value] ??= 1; input.value = pretty(current); }));
  }
  /** Capture the visible text conversation as a reviewable draft, never silently save it. */
  async function captureConversation() {
    const conversation = [];
    for (const turn of document.querySelectorAll("#conversation .turn")) {
      const bubble = turn.querySelector(".bubble");
      if (!bubble) continue;
      if (turn.classList.contains("user")) conversation.push({userContent: {role: "user", parts: [{text: bubble.textContent}]}, finalResponse: {role: "model", parts: [{text: ""}]}});
      else if (conversation.length && bubble.markdownSource) conversation[conversation.length - 1].finalResponse.parts[0].text += bubble.markdownSource;
    }
    if (!conversation.length) throw new Error("Send a message in Playground before saving an eval");
    let suite;
    const form = modal("Save conversation as eval", "Review case", async () => {
      const target = authoring.suites.find(item => item.path === suite.value);
      byId("builder-dialog").close();
      // Open after the chooser has closed so its submit lifecycle cannot dismiss the case editor.
      setTimeout(() => editCase(target, null, false, conversation).catch(showError), 0);
    });
    const destination = section(form, "Save to suite");
    suite = choices(destination, "Destination suite", [["", "Create a new suite"], ...authoring.suites.map(item => [item.path, parsedSuite(item).name || item.path])]);
    form.append(el("p", `${conversation.length} conversation turn(s). Review the expected responses and add tool expectations before saving.`, "builder-note"));
  }
  return {openEvals, captureConversation, syncEvalSelection};
})();
