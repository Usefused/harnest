/* Agent-scoped architecture; authored text never becomes markup. */
const harnestStudio = (() => {
  let projection = null;
  let request = null;
  let selected = null;
  let sourceRequest = 0;
  let bound = false;
  let focused = null;
  let view = "architecture";
  const byId = (id) => document.getElementById(id);
  const studioViewQueryKey = "studioView";
  const studioViews = new Set([...document.querySelectorAll("[data-studio-view]")].map((button) => button.dataset.studioView));
  const groups = [
    ["Agents & workflows", ["agent", "graph", "join", "condition", "input", "function"]],
    ["Capabilities", ["tool", "mcp", "skill", "model", "sandbox"]],
    ["Instructions & runtime", ["instructions", "task", "schedule", "context", "lifecycle", "agent_plugin", "extension", "workspace"]],
  ];

  /** Use text nodes for every value derived from authored source. */
  function element(tag, text, className = "") {
    const node = document.createElement(tag);
    node.textContent = text;
    node.className = className;
    return node;
  }

  /** Resolve the active Studio view from the URL, falling back to Architecture. */
  function studioViewFromLocation() {
    const name = new URLSearchParams(window.location.search).get(studioViewQueryKey)?.trim() || "";
    return studioViews.has(name) ? name : "architecture";
  }

  /** Record a Studio view change so browser back/forward follows the tabs. */
  function syncStudioViewUrl(name, { push = false } = {}) {
    const url = new URL(window.location.href);
    if (name && name !== "architecture") url.searchParams.set(studioViewQueryKey, name);
    else url.searchParams.delete(studioViewQueryKey);
    window.history[push ? "pushState" : "replaceState"](window.history.state, "", url);
  }

  /** Switch the visible Studio section and keep the URL in sync. */
  function setView(name, { push = false } = {}) {
    view = name;
    syncStudioViewUrl(name, { push });
    refreshView();
  }

  /** Bind once so switching views preserves the selected component and conversation. */
  async function open(api) {
    request = api;
    if (!bound) {
      byId("studio-search").addEventListener("input", renderComponents);
      byId("studio-refresh").addEventListener("click", load);
      byId("studio-agent").addEventListener("change", () => {
        focused = byId("studio-agent").value || null;
        const block = projection.blocks.find(item => item.id === focused);
        if (block) select(block); else refreshView();
      });
      for (const button of document.querySelectorAll("[data-studio-view]")) button.addEventListener("click", () => setView(button.dataset.studioView, { push: true }));
      window.addEventListener("popstate", () => {
        if (document.querySelector("#studio-workspace").hidden || !projection) return;
        setView(studioViewFromLocation());
      });
      bound = true;
    }
    view = studioViewFromLocation();
    if (!projection) await load();
    if (typeof harnestBuilder !== "undefined") await harnestBuilder.openStudio(api, projection, focused, load);
  }

  /** Replace the whole snapshot after a reload, never merge different build identities. */
  async function load() {
    byId("studio-status").textContent = "Reading agent architecture…";
    byId("studio-refresh").disabled = true;
    sourceRequest += 1;
    try {
      const response = await request("/_harnest/studio", { method: "GET" });
      projection = await response.json();
      byId("studio-status").textContent = `${projection.blocks.length} components · ${projection.connections.length} relationships · Build ${projection.source_digest.slice(0, 12)}`;
      focused = projection.blocks.some(item => item.id === focused) ? focused : (projection.blocks.find(item => item.path === "agent.py" && item.name === "root_agent") || projection.blocks.find(item => item.path === "agent.py" && ["agent", "graph"].includes(item.kind)) || projection.blocks.find(item => ["agent", "graph"].includes(item.kind)))?.id;
      renderAgentPicker();
      refreshView();
      renderFiles();
      renderDiagnostics();
      const block = projection.blocks.find((item) => item.id === selected) || projection.blocks.find(item => item.id === focused) || projection.blocks.find((item) => ["agent", "graph"].includes(item.kind)) || projection.blocks[0];
      if (block) select(block);
      else byId("studio-detail").replaceChildren(element("p", "No static declarations were found. Explore the source files for this agent."));
      if (typeof harnestBuilder !== "undefined") await harnestBuilder.openStudio(request, projection, focused, load);
    } catch (error) {
      projection = null;
      byId("studio-canvas").replaceChildren();
      byId("studio-components").replaceChildren();
      byId("studio-files").replaceChildren();
      byId("studio-detail").replaceChildren();
      byId("studio-diagnostics").hidden = true;
      byId("studio-status").textContent = error.message;
    } finally {
      byId("studio-refresh").disabled = false;
    }
  }

  /** Group real declarations without treating capability ownership as execution order. */
  function renderComponents() {
    if (!projection) return;
    const search = byId("studio-search").value.trim().toLowerCase();
    const host = byId("studio-components");
    for (const node of byId("studio-canvas").querySelectorAll("[data-block-id]")) {
      const block = projection.blocks.find((item) => item.id === node.dataset.blockId);
      if (block) node.classList.toggle("dimmed", !`${block.name} ${block.kind} ${block.path}`.toLowerCase().includes(search));
    }
    host.replaceChildren();
    for (const [title, kinds] of groups) {
      const blocks = visibleBlocks().filter((block) => kinds.includes(block.kind) && `${block.name} ${block.kind} ${block.path}`.toLowerCase().includes(search));
      if (!blocks.length) continue;
      const section = element("section", "", "studio-group");
      section.append(element("h3", `${title} · ${blocks.length}`));
      const cards = element("div", "", "studio-cards");
      for (const block of blocks) cards.append(componentButton(block));
      section.append(cards);
      host.append(section);
    }
    if (!host.children.length) host.append(element("p", "No matching components.", "studio-empty"));
  }

  /** Give every component a keyboard-operable selection target and source identity. */
  function componentButton(block) {
    const button = element("button", "", "studio-card");
    button.type = "button";
    button.dataset.kind = block.kind;
    button.setAttribute("aria-pressed", String(block.id === selected));
    button.append(element("span", block.kind.replaceAll("_", " "), "eyebrow"), element("strong", block.name), element("small", `${block.path}:${block.line}`));
    button.addEventListener("click", () => select(block));
    return button;
  }

  /** Set SVG attributes directly so the map respects the playground's strict CSP. */
  function svgElement(tag, attributes, text = "") {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    node.textContent = text;
    return node;
  }

  /** Keep every agent navigable while opening one container at a time. */
  function renderAgentPicker() {
    const picker = byId("studio-agent");
    picker.replaceChildren();
    const all = element("option", "All components");
    all.value = "";
    picker.append(all);
    for (const block of projection.blocks.filter(item => ["agent", "graph"].includes(item.kind))) {
      const option = element("option", `${block.name} · ${block.kind} · ${block.path}`);
      option.value = block.id;
      picker.append(option);
    }
    picker.value = focused || "";
  }

  /** A graph owns its nodes; an agent exposes only its direct capabilities and delegates. */
  function visibleBlocks() {
    const owner = projection?.blocks.find(item => item.id === focused);
    if (!owner) return projection?.blocks || [];
    const ids = new Set(owner.kind === "graph" ? Object.values(owner.config.nodes || {}) : [owner.id]);
    for (const edge of projection.connections) {
      if (edge.graph === owner.id || edge.source === owner.id) { ids.add(edge.source); ids.add(edge.target); }
    }
    return projection.blocks.filter(item => ids.has(item.id) && (owner.kind !== "graph" || item.id !== owner.id));
  }

  /** Arrange graph nodes by distance from START, with deterministic fallback for cycles. */
  function mapPositions() {
    const blocks = visibleBlocks();
    const owner = projection.blocks.find(item => item.id === focused);
    const counts = [0, 0, 0];
    if (owner?.kind !== "graph") return blocks.map(block => {
      const lane = Math.max(0, groups.findIndex(([, kinds]) => kinds.includes(block.kind)));
      return {block, x: 30 + lane * 290, y: 64 + counts[lane]++ * 100};
    });
    const levels = new Map(blocks.filter(block => block.kind === "input").map(block => [block.id, 0]));
    const edges = projection.connections.filter(edge => edge.graph === owner.id && edge.kind === "workflow");
    for (let pass = 0; pass < blocks.length; pass++) for (const edge of edges) {
      if (levels.has(edge.source) && !levels.has(edge.target)) levels.set(edge.target, levels.get(edge.source) + 1);
    }
    const columns = new Map();
    return blocks.map(block => {
      const row = levels.get(block.id) ?? levels.size;
      const column = columns.get(row) || 0;
      columns.set(row, column + 1);
      return {block, x: 30 + column * 290, y: 64 + row * 100};
    });
  }

  /** Switch Studio sections without resetting selection or touching the chat session. */
  function refreshView() {
    byId("studio-toolbar").hidden = view === "connections";
    byId("studio-search").parentElement && (byId("studio-search").parentElement.hidden = view !== "architecture");
    byId("studio-architecture").hidden = view !== "architecture";
    byId("studio-tools").hidden = view !== "tools";
    byId("mcp-workspace").hidden = view !== "connections";
    byId("studio-configuration").hidden = view !== "configuration";
    for (const button of document.querySelectorAll("[data-studio-view]")) button.setAttribute("aria-pressed", String(button.dataset.studioView === view));
    const owner = projection?.blocks.find(item => item.id === focused);
    byId("studio-scope").textContent = owner ? `${owner.name} / ${owner.kind === "graph" ? "Workflow — click a nested agent to open it" : "Capabilities and subagents"}` : "All source components";
    renderComponents();
    renderMap();
    if (typeof harnestBuilder !== "undefined") harnestBuilder.renderStudio(projection, focused);
  }

  /** Draw only resolved relationships; solid arrowheads belong to explicit workflows. */
  function mapEdge(edge, positions) {
    const from = positions.find((item) => item.block.id === edge.source);
    const to = positions.find((item) => item.block.id === edge.target);
    if (!from || !to) return null;
    const sameLane = from.x === to.x;
    const x1 = from.x + 230, y1 = from.y + 34;
    const x2 = sameLane ? to.x + 230 : to.x, y2 = to.y + 34;
    const bend = sameLane ? x1 + 32 : (x1 + x2) / 2;
    const path = svgElement("path", { d: `M ${x1} ${y1} C ${bend} ${y1}, ${bend} ${y2}, ${x2} ${y2}`, class: `studio-edge ${edge.kind || "workflow"}` });
    if (edge.kind === "workflow") path.setAttribute("marker-end", "url(#studio-arrow)");
    path.append(svgElement("title", {}, `${from.block.name} · ${relationLabel(edge)} · ${to.block.name}`));
    return path;
  }

  /** Expose SVG nodes as accessible buttons with the same inspector as the list. */
  function mapNode(position) {
    const { block, x, y } = position;
    const node = svgElement("g", { transform: `translate(${x} ${y})`, class: "studio-map-node", role: "button", tabindex: "0", "aria-label": `${block.kind}: ${block.name}`, "aria-pressed": String(block.id === selected) });
    node.dataset.blockId = block.id;
    node.append(svgElement("rect", { width: 230, height: 68, rx: 10 }), svgElement("text", { x: 14, y: 23, class: "studio-map-kind" }, block.kind.replaceAll("_", " ")), svgElement("text", { x: 14, y: 47 }, block.name.length > 25 ? `${block.name.slice(0, 24)}…` : block.name));
    node.append(svgElement("title", {}, `${block.name} · ${block.path}:${block.line}`));
    node.addEventListener("click", () => select(block));
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(block); }
    });
    return node;
  }

  /** Build a source-derived architecture map, retaining list access for every node. */
  function renderMap() {
    const positions = mapPositions();
    const height = Math.max(230, ...positions.map((item) => item.y + 100));
    const width = Math.max(300, ...positions.map(item => item.x + 260));
    const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "group", "aria-label": "Agent architecture relationships" });
    const defs = svgElement("defs", {});
    const arrow = svgElement("marker", { id: "studio-arrow", viewBox: "0 0 10 10", refX: 10, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
    arrow.append(svgElement("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "studio-arrow" }));
    defs.append(arrow);
    svg.append(defs);
    (projection.blocks.find(item => item.id === focused)?.kind === "graph" ? [["Workflow"]] : groups).forEach(([title], index) => svg.append(svgElement("text", { x: 30 + index * 290, y: 30, class: "studio-lane" }, title)));
    for (const edge of projection.connections) {
      const path = mapEdge(edge, positions);
      if (path) svg.append(path);
    }
    for (const position of positions) svg.append(mapNode(position));
    byId("studio-canvas").replaceChildren(svg);
  }

  /** Resolve relation labels independently from their visual presentation. */
  function relationLabel(edge) {
    if (edge.kind === "capability") return "Uses capability";
    if (edge.kind === "delegation") return "Delegates to";
    return edge.route === undefined ? "Workflow →" : `Workflow (${JSON.stringify(edge.route)}) →`;
  }

  /** Include a graph’s owned workflow edges alongside direct component relationships. */
  function renderRelations(block, host) {
    const edges = projection.connections.filter((edge) => edge.source === block.id || edge.target === block.id || edge.graph === block.id);
    if (!edges.length) {
      host.append(element("p", "No statically resolved connections."));
      return;
    }
    host.append(element("h4", "Relationships"));
    for (const edge of edges) {
      const incoming = edge.target === block.id;
      const otherId = incoming ? edge.source : edge.target;
      const other = projection.blocks.find((item) => item.id === otherId);
      // Graph containers own edges between their nodes without being an endpoint.
      const owned = edge.source !== block.id && edge.target !== block.id;
      const source = projection.blocks.find((item) => item.id === edge.source);
      const label = owned
        ? `${source?.name || edge.source} · ${relationLabel(edge)} ${other?.name || otherId}`
        : `${incoming ? "From " : ""}${other?.name || otherId} · ${relationLabel(edge)}`;
      const button = element("button", label, "studio-relation");
      button.type = "button";
      button.disabled = !other;
      button.dataset.kind = edge.kind || "workflow";
      if (other) button.addEventListener("click", () => select(other));
      host.append(button);
    }
  }

  /** Inspect configuration and source location without changing the running agent. */
  function select(block) {
    selected = block.id;
    if (["agent", "graph"].includes(block.kind)) {
      focused = block.id;
      byId("studio-agent").value = focused;
      refreshView();
    }
    sourceRequest += 1;
    const host = byId("studio-detail");
    host.replaceChildren(element("span", block.kind.replaceAll("_", " "), "eyebrow"), element("h3", block.name), element("p", `${block.path}:${block.line}`, "studio-location"));
    const {nodes, ...details} = block.config;
    if (Object.keys(details).length) host.append(element("pre", JSON.stringify(details, null, 2), "studio-config"));
    renderRelations(block, host);
    appendSourceButton(host, block.path);
    renderComponents();
    for (const node of byId("studio-canvas").querySelectorAll("[data-block-id]")) node.setAttribute("aria-pressed", String(node.dataset.blockId === selected));
  }

  /** Offer raw source only when the server has verified local developer access. */
  function appendSourceButton(host, path) {
    if (!projection.source_available) {
      host.append(element("p", "Source previews are available in local development.", "studio-footnote"));
      return;
    }
    const button = element("button", "View source", "secondary-button");
    button.type = "button";
    const output = element("pre", "", "studio-source");
    output.hidden = true;
    button.addEventListener("click", async () => {
      const ticket = ++sourceRequest;
      button.disabled = true;
      output.hidden = false;
      output.textContent = "Reading source…";
      try {
        const response = await request(`/_harnest/studio/source?path=${encodeURIComponent(path)}`, { method: "GET" });
        const source = await response.json();
        if (ticket === sourceRequest) output.textContent = source.text;
      } catch (error) {
        if (ticket === sourceRequest) output.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
    host.append(button, output);
    if (typeof harnestBuilder !== "undefined") harnestBuilder.appendEdit(host, path);
  }

  /** Keep file navigation constrained to the server's indexed source list. */
  function renderFiles() {
    const host = byId("studio-files");
    host.replaceChildren();
    for (const file of projection.files) {
      const button = element("button", file.path, "studio-file");
      button.type = "button";
      button.addEventListener("click", () => {
        sourceRequest += 1;
        selected = null;
        const detail = byId("studio-detail");
        detail.replaceChildren(element("span", "Source file", "eyebrow"), element("h3", file.path), element("p", `${file.language} · ${file.size} bytes`));
        appendSourceButton(detail, file.path);
        renderComponents();
      });
      host.append(button);
    }
  }

  /** Explain incomplete inspection without claiming dynamic wiring was resolved. */
  function renderDiagnostics() {
    const host = byId("studio-diagnostics");
    host.hidden = !projection.diagnostics.length;
    const list = host.querySelector("ul");
    list.replaceChildren();
    for (const item of projection.diagnostics) list.append(element("li", `${item.path}:${item.line} · ${item.message}`));
  }

  return { open };
})();
