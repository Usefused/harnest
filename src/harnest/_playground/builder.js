/* Local authoring UI. Source and provider values are always rendered as text. */
const harnestBuilder = (() => {
  let api, authoring = {available: false, scopes: [], suites: []}, projection, focus, reloadStudio;
  let buildRequest = 0;
  let bound = false, evalBound = false, selectedSuite = "", tools = [], toolConnection = "", evalCatalog;
  let connectorsAvailable = false, connectorsConnected = false;
  const byId = id => document.getElementById(id);
  const pretty = value => JSON.stringify(value, null, 2);
  const clone = value => JSON.parse(JSON.stringify(value));
  const scopeOf = path => path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ".";
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
    if (!dialog.open) byId("studio-status").textContent = error.message || String(error);
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
    byId("studio-status").textContent = message;
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
          if (reloadStudio) await reloadStudio();
          byId("eval-authoring-status").textContent = "Saved and reloaded. The updated suite is ready to run.";
          if (typeof loadEvals === "function") await loadEvals();
          return;
        }
      } catch (_) { /* The local server briefly disconnects during generation replacement. */ }
    }
    const note = "Source saved; a new build is not active yet. Check the development terminal for compilation errors, then refresh.";
    byId("studio-status").textContent = note; byId("eval-authoring-status").textContent = note;
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
  function appendEdit(host, path) {
    if (authoring.available) host.append(button("Edit source", () => editSource(path)));
  }

  async function openStudio(request, snapshot, selected, reload) {
    api = request; projection = snapshot; focus = selected; reloadStudio = reload;
    await refreshAuthoring();
    await refreshConnectors();
    byId("studio-access").textContent = authoring.available ? "Local builder" : "Read only";
    if (!bound) {
      byId("connect-fused-workspace").addEventListener("click", () => Promise.resolve().then(connectWorkspace).catch(showError));
      byId("add-mcp").addEventListener("click", () => addConnection());
      byId("browse-existing-connector").addEventListener("click", () => Promise.resolve().then(browseExistingConnector).catch(showError));
      byId("create-connector").addEventListener("click", () => Promise.resolve().then(browseNewConnector).catch(showError));
      byId("mcp-query").addEventListener("submit", queryConnection);
      byId("mcp-tool-search").addEventListener("input", renderRemoteTools);
      bound = true;
    }
    renderStudio(snapshot, selected);
  }
  /** Refresh the Fused connector state and show the right actions for it. */
  async function refreshConnectors() {
    try {
      const status = await json("/_harnest/connectors");
      connectorsAvailable = status.available;
      connectorsConnected = status.connected;
    } catch (_) { connectorsAvailable = false; connectorsConnected = false; }
    renderConnectorActions(true);
  }
  /** Present connect, add, and create actions according to OAuth state and write access. */
  function renderConnectorActions(canWriteConnection) {
    const configured = connectorsAvailable, connected = connectorsConnected;
    byId("connect-fused-workspace").hidden = !(configured && !connected);
    byId("browse-existing-connector").hidden = !(configured && connected);
    byId("create-connector").hidden = !(configured && connected);
    const status = byId("fused-connection-status");
    status.hidden = !configured;
    if (configured) {
      status.textContent = connected
        ? "Connected to your Fused workspace."
        : "Connect your Fused workspace to list or create Fused MCP servers.";
    }
    if (configured && connected) {
      byId("browse-existing-connector").disabled = !canWriteConnection;
      byId("create-connector").disabled = !canWriteConnection;
    }
  }
  /** Navigate to the Engine's consent screen; the callback stores the token. */
  async function connectWorkspace() {
    const started = await json("/_harnest/connectors/oauth/start", "POST");
    window.location.href = started.url;
  }
  function renderStudio(snapshot, selected) {
    if (!api || !snapshot) return;
    projection = snapshot; focus = selected;
    const owner = projection.blocks.find(item => item.id === focus);
    renderAttachedTools(owner);
    renderConnections();
    const config = byId("studio-configuration"); config.replaceChildren();
    if (!owner) { config.append(el("p", "Select an agent to edit its configuration.", "builder-note")); return; }
    const card = el("section", "", "builder-card");
    card.append(el("h3", owner.name), el("p", `${owner.kind} · ${owner.path}`));
    const actions = el("div", "", "builder-actions");
    if (authoring.available) {
      actions.append(button(owner.kind === "graph" ? "Edit workflow" : "Edit configuration", () => editForm(owner)), button("Edit source", () => editSource(owner.path)));
      const instructions = scoped(scopeOf(owner.path), "instructions.md");
      if (authoring.files.includes(instructions)) actions.append(button("Edit instructions", () => editSource(instructions)));
    } else card.append(el("p", "Start this agent with harnest serve --reload to enable editing.", "builder-note"));
    card.append(actions); config.append(card);
    if (authoring.available) {
      const files = el("details", "", "builder-advanced"); files.append(el("summary", "Workspace source files"));
      const list = el("div", "", "builder-file-list");
      for (const path of authoring.files) list.append(button(path, () => editSource(path)));
      files.append(list); config.append(files);
    }
  }
  function renderAttachedTools(owner) {
    const host = byId("studio-tools"); host.replaceChildren();
    const owned = new Set([...(owner?.kind === "graph" ? Object.values(owner.config.nodes || {}) : []), ...projection.connections.filter(edge => !owner || edge.source === owner.id).map(edge => edge.target)]);
    const blocks = projection.blocks.filter(item => ["tool", "mcp"].includes(item.kind) && (!owner || owned.has(item.id)));
    host.append(el("p", owner ? `Tools and MCP connections attached to ${owner.name}. Select a nested agent to inspect its own tools.` : "Tools and MCP connections across this workspace.", "builder-note"));
    const cards = el("div", "", "builder-cards");
    for (const block of blocks) {
      const card = el("section", "", "builder-card");
      card.append(el("h3", block.name), el("p", `${block.kind === "mcp" ? "MCP connection" : "Local tool"} · ${block.path}`));
      if (block.kind === "mcp") card.append(button("Find remote tools", () => discoverTools(block)));
      appendEdit(card, block.path); cards.append(card);
    }
    if (!blocks.length) cards.append(el("p", "No directly attached tools were resolved for this selection.", "builder-note"));
    host.append(cards);
  }
  function renderConnections() {
    const host = byId("studio-connections"); host.replaceChildren(); host.className = "builder-cards";
    const select = byId("mcp-client"), prior = select.value;
    select.replaceChildren();
    const connections = projection.blocks.filter(item => item.kind === "mcp");
    for (const block of connections) {
      const option = el("option", `${block.name} · ${block.path}`); option.value = block.path; select.append(option);
      const card = el("section", "", "builder-card");
      card.append(el("h3", block.name), el("p", `Agent scope: ${scopeOf(scopeOf(block.path))} · ${block.path}`));
      const actions = el("div", "", "builder-actions");
      const browse = button("Browse tools", () => discoverTools(block)); browse.className = "secondary-button compact-button";
      actions.append(browse);
      if (authoring.available) actions.append(actionMenu(`Actions for ${block.name}`, [["Select tools", () => selectTools(block)], ["Edit connection", () => editSource(block.path)], ["Remove", () => removeConnection(block)]]));
      card.append(actions); host.append(card);
    }
    if (connections.some(item => item.path === prior)) select.value = prior;
    if (!connections.length) host.append(el("p", "No MCP connections in this build. Add a connection to an agent to get started.", "builder-note"));
    // Writing a connection (manual or Fused-discovered) always ends at the same authoring
    // endpoint, so every entry point shares this gate. Without it, the Fused flows could run
    // `fused-cli init --mcp` (a real, billable provisioning call) and then fail to save the
    // resulting client file, leaving an orphaned server with nothing pointing at it.
    const canWriteConnection = authoring.available && !!(authoring.connectionScopes || []).length;
    byId("add-mcp").disabled = !canWriteConnection;
    renderConnectorActions(canWriteConnection);
    if (!authoring.available) host.append(el("p", "Start this agent with harnest serve --reload to add connections.", "builder-note"));
    else if (!(authoring.connectionScopes || []).length) host.append(el("p", "MCP connections are consumed by Agent nodes. Add an Agent to this workflow before attaching a connection.", "builder-note"));
  }
  /** ``defaults`` prefills a connection discovered through Fused; every field stays editable. */
  async function addConnection(defaults = {}) {
    let scope, name, transport, endpoint, args, token;
    const form = modal("Add MCP connection", "Add connection", async () => {
      await json("/_harnest/authoring/mcp", "POST", {scope: scope.value, name: name.value, transport: transport.value, endpoint: endpoint.value, arguments: JSON.parse(args.value || "[]"), token_env: token.value});
      await saved();
    });
    const owner = projection.blocks.find(item => item.id === focus);
    const connection = section(form, "Connection details");
    scope = choices(connection, "Attach to agent scope", (authoring.connectionScopes || []).map(value => [value, value === "." ? "Root agent" : value]), owner ? scopeOf(owner.path) : ".");
    name = field(connection, "Connection name", defaults.name || ""); name.required = true;
    transport = choices(connection, "Transport", [["streamable_http", "Streamable HTTP"], ["stdio", "Local command (stdio)"]], defaults.transport || "streamable_http");
    const server = section(form, "Server settings");
    endpoint = field(server, "Server URL", defaults.endpoint || ""); endpoint.required = true;
    args = field(server, "Command arguments (JSON array)", "[]");
    token = field(server, "Bearer token environment variable (optional)", defaults.tokenEnv || "");
    const updateTransport = () => {
      const stdio = transport.value === "stdio";
      endpoint.parentElement.firstChild.textContent = stdio ? "Command" : "Server URL";
      args.parentElement.hidden = !stdio; token.parentElement.hidden = stdio;
    };
    transport.addEventListener("change", updateTransport); updateTransport();
    if (defaults.token) {
      const secret = section(form, "Execution token", "Copy this now; it will not be shown again.");
      const secretField = field(secret, "One-time token", defaults.token);
      secretField.readOnly = true;
      secretField.addEventListener("click", () => secretField.select());
    }
    form.append(el("p", defaults.note || "Use an environment variable name for credentials. The connection belongs to the selected agent scope.", "builder-note"));
  }
  /** Resolve an already-deployed Fused MCP server, then hand its URL to addConnection. */
  async function browseExistingConnector() {
    let server, version;
    const status = el("p", "Reading deployed servers…", "builder-note");
    const form = modal("Add an existing Fused MCP server", "Continue", async () => {
      if (!server.value || !version.value) throw new Error("Choose a server and version");
      const result = await json("/_harnest/connectors/add", "POST", {name: server.value, version: version.value});
      // The modal submit handler closes this dialog after `save` resolves, so
      // defer the follow-up modal until that close has completed.
      setTimeout(() => addConnection({
        name: server.value, endpoint: result.url, tokenEnv: result.tokenEnv, token: result.token,
        note: result.token
          ? `Set ${result.tokenEnv} to the token above before using this connection.`
          : `Using the existing ${result.tokenEnv} from your environment; no new token was generated.`,
      }), 0);
    });
    const picker = section(form, "Deployed MCP servers");
    server = choices(picker, "Server", []);
    version = choices(picker, "Version", []);
    form.append(status);
    const loadVersions = async () => {
      version.replaceChildren();
      if (!server.value) return;
      const listed = await json(`/_harnest/connectors/servers/${encodeURIComponent(server.value)}/versions`);
      for (const item of (listed.items || listed || [])) { const option = el("option", item.version); option.value = item.version; version.append(option); }
    };
    try {
      const listed = await json("/_harnest/connectors/servers");
      const servers = listed.items || listed || [];
      server.replaceChildren();
      for (const item of servers) { const option = el("option", item.name); option.value = item.name; server.append(option); }
      server.addEventListener("change", () => loadVersions().catch(error => status.textContent = error.message));
      status.textContent = servers.length ? "" : "No deployed MCP servers were found for this workspace.";
      if (servers.length) await loadVersions();
    } catch (error) { status.textContent = error.message; }
  }
  /** Deploy a new Fused MCP server by picking workspace services and operations in the Studio. */
  async function browseNewConnector() {
    let name, description;
    const selections = [];
    const status = el("p", "Reading workspace services…", "builder-note");
    const form = modal("Create a new Fused MCP server", "Create", async () => {
      const services = selections
        .filter(entry => entry.box.checked)
        .map(entry => {
          const all = entry.allBox.checked;
          const operations = all ? [] : entry.opBoxes.filter(box => box.checked).map(box => box.value);
          // A narrowed service must select at least one operation; otherwise
          // the Engine would silently treat an empty allowlist as select_all.
          if (!all && !operations.length) throw new Error(`Choose operations for ${entry.service.name} or keep "All operations" selected`);
          return {slug: entry.service.slug, version: entry.service.version, selectAll: all, operations};
        });
      if (!name.value.trim()) throw new Error("Name the server first");
      if (!services.length) throw new Error("Select at least one workspace service");
      const result = await json("/_harnest/connectors/create", "POST", {
        name: name.value.trim(), description: description.value.trim(), services, tokenName: "studio",
      });
      // The modal submit handler closes this dialog after `save` resolves, so
      // defer the follow-up modal until that close has completed.
      setTimeout(() => addConnection({
        name: result.name, endpoint: result.url, tokenEnv: result.tokenEnv, token: result.token,
        note: result.token
          ? `Set ${result.tokenEnv} to the token above before using this connection.`
          : `Using the existing ${result.tokenEnv} from your environment; no new token was generated.`,
      }), 0);
    });
    const details = section(form, "Server details");
    name = field(details, "Server name"); name.required = true; name.placeholder = "my-mcp-server";
    description = field(details, "Description (optional)");
    description.placeholder = "What this server provides";
    const picker = section(form, "Workspace services", "Pick services, then narrow each one to specific operations if needed.");
    const list = el("div", "", "builder-service-list");
    picker.append(list);
    form.append(status);
    const loadOperations = async entry => {
      try {
        const version = entry.service.version_id || entry.service.version || "";
        const listed = await json(`/_harnest/connectors/services/${encodeURIComponent(entry.service.id)}/operations?version=${encodeURIComponent(version)}`);
        const items = listed.items || listed || [];
        entry.opList.replaceChildren();
        entry.opBoxes.length = 0;
        for (const op of items) {
          const row = el("label", "", "builder-operation-row");
          const box = el("input", ""); box.type = "checkbox"; box.value = op.name || "";
          row.append(box, el("span", `${op.name} · ${op.method} ${op.path}`));
          entry.opList.append(row); entry.opBoxes.push(box);
        }
        if (!items.length) entry.opList.append(el("p", "No operations were found for this service.", "builder-note"));
        entry.allBox.dispatchEvent(new Event("change"));
      } catch (error) { entry.opList.replaceChildren(el("p", error.message, "builder-note")); }
    };
    const buildServices = items => {
      list.replaceChildren(); selections.length = 0;
      for (const item of items) {
        const entry = {service: item, loaded: false, opBoxes: [], opList: null};
        const row = el("label", "", "builder-service-row");
        const box = el("input", ""); box.type = "checkbox";
        row.append(box, el("span", `${item.name} (${item.slug} · v${item.version || "?"})`));
        list.append(row);
        const ops = el("div", "", "builder-service-operations"); ops.hidden = true;
        const allRow = el("label", "", "builder-operation-row builder-operation-all");
        const allBox = el("input", ""); allBox.type = "checkbox"; allBox.checked = true;
        allRow.append(allBox, el("span", "All operations"));
        const opList = el("div", "", "builder-operation-list");
        ops.append(allRow, opList); list.append(ops);
        entry.box = box; entry.allBox = allBox; entry.opList = opList;
        selections.push(entry);
        box.addEventListener("change", () => {
          ops.hidden = !box.checked;
          // Lazy-load the operation catalogue the first time a service is selected.
          if (box.checked && !entry.loaded) { entry.loaded = true; loadOperations(entry); }
        });
        const sync = () => { for (const ob of entry.opBoxes) ob.disabled = allBox.checked; };
        allBox.addEventListener("change", sync);
      }
      if (!items.length) list.append(el("p", "No workspace services were found.", "builder-note"));
    };
    try {
      const listed = await json("/_harnest/connectors/services");
      buildServices(listed.items || listed || []);
      status.textContent = "";
    } catch (error) { status.textContent = error.message; }
  }
  async function removeConnection(block) {
    const document = await json(`/_harnest/authoring/document?path=${encodeURIComponent(block.path)}`);
    const form = modal(`Remove ${block.name}?`, "Remove connection", async () => {
      await json("/_harnest/authoring/mcp", "DELETE", {path: block.path, revision: document.revision}); await saved();
    });
    form.append(el("p", `This removes ${block.path} from this agent's workspace. It does not delete the remote MCP server.`, "builder-note"));
  }
  async function discoverTools(block) {
    toolConnection = block.path;
    byId("mcp-tool-catalog").hidden = false;
    byId("mcp-tool-catalog").querySelector("h3").textContent = `${block.name} / Server tools`;
    const host = byId("mcp-tools");
    host.replaceChildren(el("p", "Reading tool catalogue…", "builder-note"));
    tools = [];
    document.querySelector('[data-studio-view="connections"]').click();
    byId("mcp-client").value = block.path;
    byId("mcp-operation").value = "list_tools";
    try {
      const result = await json("/_harnest/studio/mcp/query", "POST", {path: block.path, operation: "list_tools"});
      tools = result.tools || result.result?.tools || [];
      byId("mcp-results").textContent = pretty(result);
      byId("mcp-cursor").value = result.nextCursor || "";
    } catch (error) {
      // Surface the failure in place instead of leaving the loading label behind.
      host.replaceChildren(el("p", error.message || "MCP discovery failed", "builder-error"));
      tools = [];
      return;
    }
    renderRemoteTools();
  }
  function renderRemoteTools() {
    const host = byId("mcp-tools"); host.replaceChildren();
    const query = byId("mcp-tool-search").value.toLowerCase();
    for (const tool of tools.filter(item => `${item.name} ${item.description || ""}`.toLowerCase().includes(query))) {
      const card = el("section", "", "builder-card");
      card.append(el("h3", tool.name), el("p", tool.description || "No description"));
      const details = el("details"), summary = el("summary", "Input schema");
      details.append(summary, el("pre", pretty(tool.inputSchema || {}), "studio-config")); card.append(details); host.append(card);
    }
    if (toolConnection && !host.children.length) host.append(el("p", "No matching tools on this catalogue page.", "builder-note"));
  }
  /** Preserve already-selected tools when a server catalogue spans multiple pages. */
  async function selectTools(block) {
    const document = await json(`/_harnest/authoring/form?path=${encodeURIComponent(block.path)}&line=${block.line}`);
    if (!("tools" in document.fields)) return editSource(block.path);
    const result = await json("/_harnest/studio/mcp/query", "POST", {path: block.path, operation: "list_tools"});
    const current = document.fields.tools;
    const available = new Map((result.tools || []).map(tool => [tool.name, tool.description || ""]));
    for (const name of current || []) if (!available.has(name)) available.set(name, "Currently selected; not listed on this catalogue page.");
    let mode;
    const selections = [];
    const form = modal(`Tools for ${block.name}`, "Save tool selection", async () => {
      const enabled = mode.value === "all" ? null : selections.filter(item => item.input.checked).map(item => item.name);
      await json("/_harnest/authoring/form", "PUT", {path: block.path, revision: document.revision, line: block.line, fields: {tools: enabled}, workflow: null});
      await saved();
    });
    const access = section(form, "Tool access", "Choose the tools this connection makes available to its agent.");
    mode = choices(access, "Enabled tools", [["all", "All server tools"], ["selected", "Only selected tools"]], current === null ? "all" : "selected");
    const catalogue = section(form, "Server tools");
    const search = field(catalogue, "Find a tool"); search.type = "search";
    for (const [name, description] of available) {
      const row = el("label", "", "builder-tool-choice");
      const input = el("input"); input.type = "checkbox"; input.checked = current === null || current.includes(name);
      const copy = el("span"); copy.append(el("strong", name), el("span", description, "builder-note"));
      row.append(input, copy); catalogue.append(row); selections.push({name, input, row, description});
    }
    const update = () => { for (const item of selections) { item.input.disabled = mode.value === "all"; item.row.hidden = !`${item.name} ${item.description}`.toLowerCase().includes(search.value.toLowerCase()); } };
    mode.addEventListener("change", update); search.addEventListener("input", update); update();
    if (result.nextCursor) catalogue.append(el("p", "Showing one catalogue page. Existing selections from other pages are retained; Edit connection supports additional names.", "builder-note"));
  }
  async function queryConnection(event) {
    event.preventDefault();
    const submit = event.currentTarget.querySelector('button[type="submit"]'); submit.disabled = true;
    try {
      const result = await json("/_harnest/studio/mcp/query", "POST", {path: byId("mcp-client").value, operation: byId("mcp-operation").value, identifier: byId("mcp-identifier").value, arguments: JSON.parse(byId("mcp-arguments").value || "{}"), cursor: byId("mcp-cursor").value || null});
      byId("mcp-results").textContent = pretty(result);
      if (byId("mcp-operation").value === "list_tools") { byId("mcp-tool-catalog").hidden = false; tools = result.tools || []; toolConnection = byId("mcp-client").value; renderRemoteTools(); }
      byId("mcp-cursor").value = result.nextCursor || "";
    } catch (error) { byId("mcp-results").textContent = error.message; }
    finally { submit.disabled = false; }
  }
  async function editForm(block) {
    const document = await json(`/_harnest/authoring/form?path=${encodeURIComponent(block.path)}&line=${block.line}`);
    const inputs = {};
    let readWorkflow;
    const form = modal(block.kind === "graph" ? `Edit ${block.name} workflow` : `Configure ${block.name}`, "Save changes", async () => {
      const fields = {};
      for (const [name, value] of Object.entries(document.fields)) fields[name] = typeof value === "string" ? inputs[name].value : JSON.parse(inputs[name].value);
      await json("/_harnest/authoring/form", "PUT", {path: block.path, revision: document.revision, line: block.line, fields, workflow: readWorkflow ? readWorkflow() : null});
      await saved();
    });
    const settings = section(form, block.kind === "graph" ? "Workflow settings" : "Agent settings");
    for (const [name, value] of Object.entries(document.fields)) inputs[name] = field(settings, name === "tools" ? 'Enabled tool names (JSON array, [] for none, null for all)' : name.replaceAll("_", " "), typeof value === "string" ? value : pretty(value));
    if (document.workflow) readWorkflow = workflowFields(form, document.workflow);
    if (!Object.keys(document.fields).length && !document.workflow) form.append(el("p", "This declaration uses dynamic values. Use Edit source to change its wiring.", "builder-note"));
  }
  /** Edit node references and routing as rows, preserving unsupported Python in source mode. */
  function workflowFields(form, workflow) {
    const nodeRows = [], edgeRows = [], nodes = el("div"), edges = el("div");
    const nodeSection = section(form, "Workflow nodes", "Each node refers to a Python symbol in this source file.");
    nodeSection.append(nodes);
    const addNode = (name = "", reference = "") => {
      const row = el("div", "", "builder-row");
      const key = field(row, "Node name", name), symbol = field(row, "Python symbol", reference);
      row.append(button("Remove node", () => { row.remove(); nodeRows.splice(nodeRows.indexOf(entry), 1); }));
      const entry = {key, symbol}; nodeRows.push(entry); nodes.append(row);
    };
    for (const [name, value] of Object.entries(workflow.nodes)) addNode(name, value);
    nodeSection.append(button("Add node", () => addNode()));
    const edgeSection = section(form, "Connections", "Connect nodes in execution order, with optional routes.");
    edgeSection.append(edges);
    const addEdge = (value = {}) => {
      const row = el("div", "", "builder-row builder-edge");
      const source = field(row, "From", value.source || "START"), target = field(row, "To", value.target || "");
      const route = field(row, "Route (JSON, null for always)", pretty(value.route ?? null));
      row.append(button("Remove edge", () => { row.remove(); edgeRows.splice(edgeRows.indexOf(entry), 1); }));
      const entry = {source, target, route}; edgeRows.push(entry); edges.append(row);
    };
    workflow.edges.forEach(addEdge);
    edgeSection.append(button("Add edge", () => addEdge()));
    return () => ({nodes: Object.fromEntries(nodeRows.map(({key, symbol}) => [key.value, symbol.value])), edges: edgeRows.map(({source, target, route}) => ({source: source.value, target: target.value, route: JSON.parse(route.value)}))});
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
  return {openStudio, renderStudio, appendEdit, openEvals, captureConversation, syncEvalSelection};
})();
