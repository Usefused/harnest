/* Agent-scoped architecture; authored text never becomes markup. */
const harnestStudio = (() => {
  let projection = null;
  let request = null;
  let selected = null;
  let agentDetail = null;
  let sourceRequest = 0;
  let bound = false;
  let focused = null;
  let view = "architecture";
  const byId = (id) => document.getElementById(id);
  const studioViewQueryKey = "studioView";
  const studioAgentQueryKey = "studioAgent";
  const studioViews = new Set([...document.querySelectorAll("[data-studio-view]")].map((button) => button.dataset.studioView));
  const groups = [
    ["Agents & workflows", ["agent", "graph", "join", "condition", "input", "function"]],
    ["Capabilities", ["tool", "mcp", "skill", "model", "sandbox"]],
    ["Instructions & runtime", ["instructions", "task", "schedule", "context", "workspace"]],
  ];
  const sidePanelKinds = [
    ["Extensions", "extension"],
    ["Agent plugins", "agent_plugin"],
    ["Static MCPs", "mcp"],
    ["Lifecycle", "lifecycle"],
  ];
  const topLevelKinds = new Set(["agent", "graph", "join"]);
  const topLevelEdgeKinds = new Set(["workflow", "delegation"]);
  const agentDetailKinds = new Set(["agent", "graph"]);

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

  /** Resolve a deep-linked agent/graph detail page id from the URL, if any. */
  function studioAgentFromLocation() {
    return new URLSearchParams(window.location.search).get(studioAgentQueryKey)?.trim() || null;
  }

  /** Record the open agent detail page so browser back/forward follows it. */
  function syncStudioAgentUrl(id, { push = false } = {}) {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set(studioAgentQueryKey, id);
    else url.searchParams.delete(studioAgentQueryKey);
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
        const id = byId("studio-agent").value;
        if (!id) { closeAgentDetail(); return; }
        const block = projection?.blocks.find(item => item.id === id);
        if (block) openAgentDetail(block);
      });
      byId("studio-agent-back").addEventListener("click", () => closeAgentDetail());
      for (const button of document.querySelectorAll("[data-studio-view]")) button.addEventListener("click", () => setView(button.dataset.studioView, { push: true }));
      window.addEventListener("popstate", () => {
        if (document.querySelector("#studio-workspace").hidden || !projection) return;
        view = studioViewFromLocation();
        const block = projection.blocks.find(item => item.id === studioAgentFromLocation() && agentDetailKinds.has(item.kind));
        agentDetail = block ? block.id : null;
        if (block) { selected = block.id; focused = block.id; byId("studio-agent").value = block.id; }
        refreshView();
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
      const deepLinked = projection.blocks.find(item => item.id === studioAgentFromLocation() && agentDetailKinds.has(item.kind));
      agentDetail = deepLinked ? deepLinked.id : null;
      focused = deepLinked ? deepLinked.id : null;
      selected = deepLinked ? deepLinked.id : null;
      renderAgentPicker();
      refreshView();
      renderFiles();
      renderDiagnostics();
      if (typeof harnestBuilder !== "undefined") await harnestBuilder.openStudio(request, projection, focused, load);
    } catch (error) {
      projection = null;
      agentDetail = null;
      byId("studio-canvas").replaceChildren();
      byId("studio-components").replaceChildren();
      byId("studio-files").replaceChildren();
      byId("studio-detail").replaceChildren();
      byId("studio-detail").hidden = true;
      byId("studio-agent-detail-body").replaceChildren();
      byId("studio-agent-detail").hidden = true;
      byId("studio-architecture-canvas-view").hidden = false;
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
    const all = element("option", "Open an agent…");
    all.value = "";
    picker.append(all);
    for (const block of projection.blocks.filter(item => agentDetailKinds.has(item.kind))) {
      const option = element("option", `${block.name} · ${block.kind} · ${block.path}`);
      option.value = block.id;
      picker.append(option);
    }
    picker.value = focused || "";
  }

  /** A graph owns its nodes; an agent exposes only its direct capabilities and delegates. */
  function ownedBlocks(owner) {
    if (!owner) return projection?.blocks || [];
    const ids = new Set(owner.kind === "graph" ? Object.values(owner.config.nodes || {}) : [owner.id]);
    for (const edge of projection.connections) {
      if (edge.graph === owner.id) { ids.add(edge.source); ids.add(edge.target); }
      // A delegated agent is a whole separate agent, not part of this one's own
      // capabilities — it stays reachable only via the Relationships list.
      else if (edge.source === owner.id && edge.kind !== "delegation") ids.add(edge.target);
    }
    return projection.blocks.filter(item => ids.has(item.id) && (owner.kind !== "graph" || item.id !== owner.id));
  }

  /** Scope ownership to whichever agent or graph is currently focused on the canvas. */
  function visibleBlocks() {
    return ownedBlocks(projection?.blocks.find(item => item.id === focused));
  }

  /** Union the lib/ modules imported by a block's own file and everything it owns. */
  function filesForAgent(block) {
    const paths = new Set([block.path, ...ownedBlocks(block).map(item => item.path)]);
    const imports = new Set();
    for (const path of paths) {
      const file = projection.files.find(item => item.path === path);
      for (const lib of file?.lib_imports || []) imports.add(lib);
    }
    return [...imports].sort();
  }

  /** Arrange a set of blocks by workflow depth: graph nodes from START, top-level flow from its roots. */
  function computePositions(blocks, owner) {
    if (owner?.kind === "graph") {
      const levels = new Map(blocks.filter(block => block.kind === "input").map(block => [block.id, 0]));
      const edges = projection.connections.filter(edge => edge.graph === owner.id && edge.kind === "workflow");
      for (let pass = 0; pass < blocks.length; pass++) for (const edge of edges) {
        if (levels.has(edge.source) && !levels.has(edge.target)) levels.set(edge.target, levels.get(edge.source) + 1);
      }
      return columnsByLevel(blocks, levels);
    }
    if (owner) {
      const counts = [0, 0, 0];
      return blocks.map(block => {
        const lane = Math.max(0, groups.findIndex(([, kinds]) => kinds.includes(block.kind)));
        return { block, x: 30 + lane * 290, y: 64 + counts[lane]++ * 100 };
      });
    }
    const ids = new Set(blocks.map(block => block.id));
    const edges = projection.connections.filter(edge => topLevelEdgeKinds.has(edge.kind) && ids.has(edge.source) && ids.has(edge.target));
    const incoming = new Set(edges.map(edge => edge.target));
    const levels = new Map(blocks.filter(block => !incoming.has(block.id)).map(block => [block.id, 0]));
    for (let pass = 0; pass < blocks.length; pass++) for (const edge of edges) {
      if (levels.has(edge.source) && !levels.has(edge.target)) levels.set(edge.target, levels.get(edge.source) + 1);
    }
    return columnsByLevel(blocks, levels);
  }

  /** Stack same-depth blocks into columns without disturbing deterministic ordering. */
  function columnsByLevel(blocks, levels) {
    const columns = new Map();
    return blocks.map(block => {
      const row = levels.get(block.id) ?? levels.size;
      const column = columns.get(row) || 0;
      columns.set(row, column + 1);
      return { block, x: 30 + column * 290, y: 64 + row * 100 };
    });
  }

  /** Switch Studio sections without resetting selection or touching the chat session. */
  function refreshView() {
    byId("studio-toolbar").hidden = view === "connections" || view === "catalog";
    byId("studio-search").parentElement && (byId("studio-search").parentElement.hidden = view !== "architecture");
    byId("studio-architecture").hidden = view !== "architecture";
    byId("studio-tools").hidden = view !== "tools";
    byId("mcp-workspace").hidden = view !== "connections";
    byId("studio-configuration").hidden = view !== "configuration";
    byId("studio-catalog").hidden = view !== "catalog";
    for (const button of document.querySelectorAll("[data-studio-view]")) button.setAttribute("aria-pressed", String(button.dataset.studioView === view));
    if (view === "architecture") {
      const block = agentDetail ? projection?.blocks.find(item => item.id === agentDetail) : null;
      if (block) {
        renderAgentDetail(block);
        byId("studio-architecture-canvas-view").hidden = true;
        byId("studio-agent-detail").hidden = false;
      } else {
        agentDetail = null;
        byId("studio-agent-detail").hidden = true;
        byId("studio-architecture-canvas-view").hidden = false;
        byId("studio-scope").textContent = "High-level agent and graph flow — click a node to open it";
        renderTopLevelMap();
      }
    } else if (view === "catalog") {
      renderCatalog();
    }
    renderComponents();
    if (typeof harnestBuilder !== "undefined") harnestBuilder.renderStudio(projection, focused);
  }

  /** Draw only the top-level agents, graphs, and joins — capability detail lives on their pages. */
  function renderTopLevelMap() {
    const blocks = (projection?.blocks || []).filter(block => topLevelKinds.has(block.kind));
    const edges = (projection?.connections || []).filter(edge => topLevelEdgeKinds.has(edge.kind));
    renderMap(byId("studio-canvas"), blocks, edges, null);
  }

  /** Draw only resolved relationships; solid arrowheads belong to explicit workflows. */
  function mapEdge(edge, positions, arrowId) {
    const from = positions.find((item) => item.block.id === edge.source);
    const to = positions.find((item) => item.block.id === edge.target);
    if (!from || !to) return null;
    const sameLane = from.x === to.x;
    const x1 = from.x + 230, y1 = from.y + 34;
    const x2 = sameLane ? to.x + 230 : to.x, y2 = to.y + 34;
    const bend = sameLane ? x1 + 32 : (x1 + x2) / 2;
    const path = svgElement("path", { d: `M ${x1} ${y1} C ${bend} ${y1}, ${bend} ${y2}, ${x2} ${y2}`, class: `studio-edge ${edge.kind || "workflow"}` });
    if (edge.kind === "workflow") path.setAttribute("marker-end", `url(#${arrowId})`);
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

  /** Build a source-derived architecture map into any host, scoped to any owner. */
  function renderMap(host, blocks, edges, owner) {
    const positions = computePositions(blocks, owner);
    const height = Math.max(230, ...positions.map((item) => item.y + 100));
    const width = Math.max(300, ...positions.map(item => item.x + 260));
    const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "group", "aria-label": owner ? `${owner.name}'s internal components` : "Agent architecture flow" });
    const defs = svgElement("defs", {});
    const arrowId = `${host.id || "studio-canvas"}-arrow`;
    const arrow = svgElement("marker", { id: arrowId, viewBox: "0 0 10 10", refX: 10, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
    arrow.append(svgElement("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "studio-arrow" }));
    defs.append(arrow);
    svg.append(defs);
    if (owner?.kind === "graph") svg.append(svgElement("text", { x: 30, y: 30, class: "studio-lane" }, "Workflow"));
    else if (owner) groups.forEach(([title], index) => svg.append(svgElement("text", { x: 30 + index * 290, y: 30, class: "studio-lane" }, title)));
    for (const edge of edges) {
      const path = mapEdge(edge, positions, arrowId);
      if (path) svg.append(path);
    }
    for (const position of positions) svg.append(mapNode(position));
    host.replaceChildren(svg);
    enablePan(host);
  }

  /** Let a wide diagram be explored by dragging it, not just scrollbars. */
  function enablePan(host) {
    if (host.dataset.panBound) return;
    host.dataset.panBound = "true";
    let dragging = false;
    let startX = 0, startY = 0, startLeft = 0, startTop = 0;
    host.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest(".studio-map-node")) return;
      dragging = true;
      startX = event.clientX;
      startY = event.clientY;
      startLeft = host.scrollLeft;
      startTop = host.scrollTop;
      host.classList.add("dragging");
      host.setPointerCapture(event.pointerId);
    });
    host.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      host.scrollLeft = startLeft - (event.clientX - startX);
      host.scrollTop = startTop - (event.clientY - startY);
    });
    const release = () => { dragging = false; host.classList.remove("dragging"); };
    host.addEventListener("pointerup", release);
    host.addEventListener("pointercancel", release);
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
    if (agentDetailKinds.has(block.kind)) { openAgentDetail(block); return; }
    if (agentDetail) closeAgentDetail({ push: false });
    selected = block.id;
    sourceRequest += 1;
    const host = byId("studio-detail");
    host.hidden = false;
    host.replaceChildren(element("span", block.kind.replaceAll("_", " "), "eyebrow"), element("h3", block.name), element("p", `${block.path}:${block.line}`, "studio-location"));
    const {nodes, ...details} = block.config;
    if (Object.keys(details).length) host.append(element("pre", JSON.stringify(details, null, 2), "studio-config"));
    renderRelations(block, host);
    appendSourceButton(host, block.path);
    renderComponents();
    for (const node of byId("studio-canvas").querySelectorAll("[data-block-id]")) node.setAttribute("aria-pressed", String(node.dataset.blockId === selected));
  }

  /** Open the full-page view of one agent or graph: its own flow, tools, and lib usage. */
  function openAgentDetail(block, { push = true } = {}) {
    agentDetail = block.id;
    selected = block.id;
    focused = block.id;
    byId("studio-agent").value = block.id;
    syncStudioAgentUrl(block.id, { push });
    renderAgentDetail(block);
    byId("studio-architecture-canvas-view").hidden = true;
    byId("studio-agent-detail").hidden = false;
  }

  /** Return from an agent's detail page to the high-level architecture canvas. */
  function closeAgentDetail({ push = true } = {}) {
    agentDetail = null;
    syncStudioAgentUrl(null, { push });
    byId("studio-agent").value = "";
    byId("studio-agent-detail").hidden = true;
    byId("studio-architecture-canvas-view").hidden = false;
    renderTopLevelMap();
  }

  /** Render one agent/graph's inner content: its own flow, tools with durability, and lib usage. */
  function renderAgentDetail(block) {
    const header = byId("studio-agent-detail-header");
    const heading = element("h2", block.name);
    heading.id = "studio-agent-detail-heading";
    header.replaceChildren(
      element("span", block.kind.replaceAll("_", " "), "eyebrow"),
      heading,
      element("p", `${block.path}:${block.line}`, "studio-location"),
    );

    const owned = ownedBlocks(block);
    const submapEdges = projection.connections.filter(edge =>
      edge.graph === block.id || edge.source === block.id ||
      owned.some(item => item.id === edge.source) || owned.some(item => item.id === edge.target));
    renderMap(byId("studio-agent-detail-map"), owned, submapEdges, block);

    const host = byId("studio-agent-detail-body");
    host.replaceChildren();
    const grid = element("div", "", "studio-agent-detail-grid");
    host.append(grid);

    const toolsSection = document.createElement("section");
    toolsSection.append(element("h4", "Tools"));
    const tools = owned.filter(item => item.kind === "tool");
    if (tools.length) {
      const cards = element("div", "", "studio-cards");
      for (const tool of tools) {
        const card = element("div", "", "studio-card");
        const name = element("strong", tool.name);
        name.append(element("span", tool.config.durable ? "Durable" : "Not durable", `studio-tool-badge ${tool.config.durable ? "durable" : "non-durable"}`));
        card.append(element("span", "tool", "eyebrow"), name, element("small", `${tool.path}:${tool.line}`));
        cards.append(card);
      }
      toolsSection.append(cards);
    } else {
      toolsSection.append(element("p", "No tools attached.", "studio-empty"));
    }
    grid.append(toolsSection);

    const libsSection = document.createElement("section");
    libsSection.append(element("h4", "Lib packages used"));
    const libs = filesForAgent(block);
    if (libs.length) {
      const list = document.createElement("ul");
      list.className = "studio-lib-list";
      for (const path of libs) {
        const item = document.createElement("li");
        item.append(element("code", path));
        appendSourceButton(item, path);
        list.append(item);
      }
      libsSection.append(list);
    } else {
      libsSection.append(element("p", "No lib/ modules imported by this component or its capabilities.", "studio-lib-empty"));
    }
    grid.append(libsSection);

    const { nodes, ...details } = block.config;
    if (Object.keys(details).length) {
      const configSection = document.createElement("section");
      configSection.append(element("h4", "Configuration"), element("pre", JSON.stringify(details, null, 2), "studio-config"));
      grid.append(configSection);
    }

    const relationsSection = document.createElement("section");
    grid.append(relationsSection);
    renderRelations(block, relationsSection);

    appendSourceButton(host, block.path);
  }

  /** List extensions, agent plugins, static MCPs, and lifecycle hooks across the whole workspace. */
  function renderCatalog() {
    const host = byId("studio-catalog");
    host.replaceChildren();
    if (!projection) return;
    for (const [title, kind] of sidePanelKinds) {
      const blocks = projection.blocks.filter(block => block.kind === kind);
      const group = element("section", "", "studio-group");
      group.append(element("h3", `${title} · ${blocks.length}`));
      if (!blocks.length) {
        group.append(element("p", `No ${title.toLowerCase()} found.`, "studio-empty"));
      } else {
        const cards = element("div", "", "studio-cards");
        for (const block of blocks) {
          const meta = kind === "lifecycle" ? (block.config.role || "lifecycle") : block.path;
          const card = element("button", "", "studio-card");
          card.type = "button";
          card.append(element("span", block.kind.replaceAll("_", " "), "eyebrow"), element("strong", block.name), element("small", meta));
          card.addEventListener("click", () => { setView("architecture", { push: true }); select(block); });
          cards.append(card);
        }
        group.append(cards);
      }
      host.append(group);
    }
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
        if (agentDetail) closeAgentDetail({ push: false });
        sourceRequest += 1;
        selected = null;
        const detail = byId("studio-detail");
        detail.hidden = false;
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
