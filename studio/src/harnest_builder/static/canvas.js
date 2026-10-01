import {preference} from "./ui.js";

const WIDTH = 220, HEIGHT = 78;
const ROW = 112;
const ICONS = {agent:"◈", instructions:"≡", config:"⚙", card:"▣", tool:"⌘", subagent:"◈", task:"↻", mcp:"⌁", skill:"✧", plugin:"⬡", extension:"▧", sandbox:"▤", cron:"◷", lifecycle:"↝", context:"◎", storage:"▱", model:"◇", library:"⌘", eval:"✓", test:"✓", smoke:"✓", channel:"↗", dependencies:"⬡", node:"◈", source:"⌘"};
export {ICONS};

/** Create SVG through attributes so the canvas works under a strict script policy. */
function svg(tag, attributes = {}, text = "") {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  node.textContent = text; return node;
}

/** Classify authored paths without pretending every folder defines execution order. */
export function kindOf(path) {
  const core = {"agent.py":"agent", "instructions.md":"instructions", "config.yaml":"config", "agent-card.yaml":"card", "pyproject.toml":"dependencies"};
  const groups = {tools:"tool", subagents:"subagent", tasks:"task", mcp:"mcp", skills:"skill", plugins:"plugin", extensions:"extension", sandbox:"sandbox", cron:"cron", lifecycle:"lifecycle", models:"model", lib:"library", evals:"eval", tests:"test", channels:"channel"};
  return core[path] || groups[path.split("/")[0]] || "source";
}

const INPUT_KINDS = new Set(["instructions", "config", "card", "dependencies"]);
const CATEGORIES = {tool:"Tools",subagent:"Subagents",mcp:"MCP connections",extension:"Extensions",skill:"Skills",plugin:"Plugins",channel:"Channels",sandbox:"Sandboxes",task:"Tasks",cron:"Schedules",lifecycle:"Lifecycle",model:"Models",library:"Libraries",eval:"Evaluations",test:"Tests",source:"Other source"};

/** Filter inert guide files while leaving all authored files available in the file browser. */
function visible(path) {
  return !path.split("/").some(part => part.startsWith("_")) && !path.endsWith(".lock");
}

/** Build directory children so an extension package stays together until explicitly opened. */
function members(files, prefix, kind) {
  const result=new Map();
  for(const path of files) {
    const relative=path.slice(prefix.length), parts=relative.split("/"), folder=parts.length>1;
    const id=folder?`folder:${prefix}${parts[0]}`:path;
    if(!result.has(id)) result.set(id,{id,path:folder?prefix+parts[0]+"/":path,title:parts[0],kind,group:folder,files:[]});
    result.get(id).files.push(path);
  }
  return [...result.values()].map(node=>node.group?{...node,children:members(node.files,node.path,kind)}:node);
}

/** Build categories inside each native agent scope, including nested subagent capabilities. */
function scopeCategories(project, owner) {
  const owners=project.ownership?.available?project.ownership.agents:[];
  const children=owners.filter(agent=>agent.id!==owner.id && agent.path.startsWith(owner.folder+"subagents/") && !agent.path.slice((owner.folder+"subagents/").length).includes("/subagents/"));
  const files=project.files.filter(path=>visible(path) && path.startsWith(owner.folder) && !children.some(agent=>path===agent.path || (!agent.flat && path.startsWith(agent.folder))));
  const categories=[];
  for(const [kind,title] of Object.entries(CATEGORIES)) {
    const paths=files.filter(path=>kindOf(path.slice(owner.folder.length))===kind);
    const prefix=kind==="source"?owner.folder:owner.folder+(paths[0]?.slice(owner.folder.length).split("/")[0]||"subagents")+"/";
    const items=members(paths,prefix,kind);
    if(kind==="subagent") for(const agent of children) {
      const nested=agent.flat?[]:scopeCategories(project,agent);
      items.push({id:agent.id,path:agent.path,title:agent.name,kind:"subagent",ownerId:agent.id,group:nested.length>0,files:project.files.filter(path=>path===agent.path||(!agent.flat&&path.startsWith(agent.folder))),children:nested});
    }
    if(!items.length) continue;
    categories.push({id:`category:${kind}${owner.id==="agent.py"?"":":"+owner.id}`,path:prefix,title,kind,group:true,files:items.flatMap(item=>item.files),children:items});
  }
  return categories;
}

/** Project actual folder ownership into collapsible categories and movable capability leaves. */
export function architectureNodes(project, expanded=new Set(), positions={}) {
  const files=project.files.filter(visible), nodes=[];
  let input=0, row=0;
  for(const path of files) {
    const kind=kindOf(path);
    if(kind!=="agent"&&!INPUT_KINDS.has(kind)) continue;
    const position=kind==="agent"?{x:345,y:205}:{x:30,y:35+input++*ROW};
    nodes.push({id:path,path,title:kind==="agent"?project.config.name:path,kind,...position,...positions[path],...(kind==="agent"?{ownerId:path}:{})});
  }
  for(const category of scopeCategories(project,{id:"agent.py",folder:""})) {
    row=layoutBranch(category,"agent.py",0,row,nodes,expanded,positions);
  }
  const root=nodes.find(node=>node.id==="agent.py");
  if(root && !positions[root.id]) root.y=35+(Math.max(input,row)-1)*ROW/2;
  for(const node of nodes) {
    node.resources=resourcesForNode(node,project.ownership?.resources||[]);
    if(node.resources.length===1) {node.resource=node.resources[0];if(!node.group)node.kind=node.resource.kind;}
  }
  return nodes;
}

/** Resolve category and package edges to complete native resources, never partial skill files. */
export function resourcesForNode(node,resources) {
  const path=node.path.replace(/\/$/,"");
  const exact=resources.find(resource=>resource.path===path||resource.anchor===path);
  if(exact) return [exact];
  const candidates=node.group?resources.filter(resource=>resource.path.startsWith(path+"/")):resources.filter(resource=>resource.kind==="skill"&&path.startsWith(resource.path+"/"));
  return candidates.filter(resource=>!candidates.some(parent=>parent!==resource&&resource.path.startsWith(parent.path+"/")));
}

/** Allocate a separate row for every visible leaf and center each owner beside its children. */
function layoutBranch(node,parent,depth,row,nodes,expanded,positions) {
  const start=row, opened=node.group&&expanded.has(node.id);
  const current={...node,parent,expanded:Boolean(opened),x:660+depth*280,y:35+row*ROW};
  nodes.push(current);
  if(opened) for(const child of node.children) row=layoutBranch(child,node.id,depth+1,row,nodes,expanded,positions);
  else row++;
  current.y=35+(start+row-1)*ROW/2;
  Object.assign(current,positions[node.id]);
  return row;
}

/** Use readable singular and plural counts in visible and accessible category labels. */
function countLabel(count, noun) { return `${count} ${noun}${count===1?"":"s"}`; }

/** Keep long source identities legible without changing their accessible labels. */
function short(value, length) { return value.length > length ? value.slice(0, length - 1) + "…" : value; }

/** Avoid folded short forward curves while retaining clearance for backward links. */
function curveBend(start,end) {
  return Math.max(end.x>=start.x?15:65,Math.abs(end.x-start.x)*.45);
}

/** Render source components with persisted layout and editable graph connections. */
export class Canvas {
  constructor(host, handlers) {
    this.host = host; this.handlers = handlers; this.zoom = 1; this.project = null; this.nodes = []; this.edges = []; this.positions = {};
    this.view = {x:0, y:0, width:1100, height:750};
    host.addEventListener("dragover", event => { if (event.dataTransfer.types.includes("application/harnest-component")) { event.preventDefault(); host.parentElement.classList.add("drop-active"); } });
    host.addEventListener("dragleave", event => { if (!host.contains(event.relatedTarget)) host.parentElement.classList.remove("drop-active"); });
    host.addEventListener("drop", event => {
      event.preventDefault(); host.parentElement.classList.remove("drop-active");
      const kind = event.dataTransfer.getData("application/harnest-component");
      if (kind) this.handlers.drop(kind, this.point(event));
    });
    new ResizeObserver(() => this.viewport()).observe(host);
  }

  /** Rebuild from source after every save; positions remain a local visual preference. */
  render(project, mode) {
    const changed = this.project?.id !== project?.id || this.mode !== mode;
    const previousCount=this.nodes.length;
    this.project = project; this.mode = mode; this.setPending(null);
    this.layoutKey = `layout:${project?.id}:${mode}${mode==="architecture"?":categories":""}`;
    this.positions = preference(this.layoutKey) || {};
    this.expanded = new Set(preference(`expanded:${project?.id}`) || []);
    this.host.replaceChildren();
    if (!project) return;
    this.nodes = mode === "workflow" && project.graph.available ? this.workflowNodes(project) : architectureNodes(project,this.expanded,this.positions);
    this.edges = mode === "workflow" && project.graph.available ? project.graph.edges.map((edge, index) => ({...edge, index})) : this.nodes.filter(n => n.id !== "agent.py").map(n => this.ownershipEdge(n));
    this.root = svg("svg", {role:"group", "aria-label":mode === "workflow" ? "Agent workflow. Connect output ports to input ports." : "Agent source architecture. Click categories to expand or collapse. Drag cards to arrange. Double-click files to edit. Drag any edge or its endpoint onto a node to validate a new connection."});
    const defs = svg("defs"), marker = svg("marker", {id:"arrow", viewBox:"0 0 10 10", refX:9, refY:5, markerWidth:5, markerHeight:5, orient:"auto-start-reverse"});
    marker.append(svg("path", {d:"M 0 0 L 10 5 L 0 10 z", fill:"context-stroke"})); defs.append(marker); this.root.append(defs);
    this.lines = svg("g"); this.cards = svg("g"); this.root.append(this.lines, this.cards);
    for (const node of this.nodes) this.cards.append(this.card(node));
    this.host.append(this.root); this.drawEdges();
    this.root.addEventListener("pointerdown", event => this.pan(event));
    this.root.addEventListener("wheel", event => { if (event.ctrlKey || event.metaKey) { event.preventDefault(); this.scale(event.deltaY > 0 ? .9 : 1.1); } }, {passive:false});
    if (changed || this.nodes.length!==previousCount) this.fit(); else this.viewport();
  }

  /** Configuration informs the agent; authored capabilities belong to its source scope. */
  ownershipEdge(node) {
    if(node.parent) return {source:node.parent,target:node.id,...(node.resource?{resource:node.resource}: {})};
    const input=INPUT_KINDS.has(node.kind);
    return {source:input?node.id:"agent.py",target:input?"agent.py":node.id};
  }

  /** Reflect explicit Graph node identities rather than inferring edges from folder order. */
  workflowNodes(project) {
    const start = {id:"START", path:"agent.py", title:"Start", kind:"node", x:30, y:90};
    const nodes = project.graph.nodes.map((node,index) => ({id:node.id,path:this.nodeSource(project,node),title:node.id,kind:"node",expression:node.expression,x:345 + (index % 3) * 295,y:90 + Math.floor(index/3) * 190}));
    return [start,...nodes].map(node => ({...node,...this.positions[node.id]}));
  }

  /** Open the discovered subagent or tool file when graph nodes refer to resource names. */
  nodeSource(project,node) {
    return ["subagents","tools"].map(folder=>`${folder}/${node.reference}.py`).find(path=>project.files.includes(path))||"agent.py";
  }

  /** Support pointer movement and keyboard source inspection on every node. */
  card(node) {
    const group = svg("g", {class:"node-card", transform:`translate(${node.x},${node.y})`, tabindex:0, role:"button", "aria-label":node.group?`${node.expanded?"Collapse":"Expand"} ${node.title}, ${countLabel(node.files.length,"file")}`:`${node.title}, ${node.kind}, ${node.path}`, "data-node":node.id});
    if(node.group) {group.classList.add("category-card");group.setAttribute("aria-expanded",String(node.expanded));}
    group.append(svg("rect", {class:"card-bg",width:WIDTH,height:HEIGHT,rx:10}));
    group.append(svg("title",{},node.path));
    group.append(svg("rect", {class:"node-icon-bg",x:14,y:15,width:30,height:30,rx:8}), svg("text", {class:"node-icon",x:21,y:36}, ICONS[node.kind] || "◇"));
    if(node.group) {
      group.append(svg("text",{class:"node-name",x:55,y:34},short(node.title,18)));
      group.append(svg("text",{class:"node-path",x:55,y:57},`${countLabel(node.children.length,"item")} · ${countLabel(node.files.length,"file")}`));
    } else {
      group.append(svg("text",{class:"node-kind",x:55,y:24},node.id==="START"?"ENTRY POINT":node.kind.toUpperCase()));
      group.append(svg("text",{class:"node-name",x:55,y:41},short(node.title,21)));
      group.append(svg("text",{class:"node-path",x:15,y:63},short(node.path,33)));
    }
    group.addEventListener("pointerdown", event => this.drag(event, node, group));
    if(node.group) group.append(svg("path",{class:"category-chevron",d:node.expanded?"M196 28 L200 32 L204 28":"M198 26 L202 30 L198 34","aria-hidden":"true"}));
    group.addEventListener("click", event => {if(node.group&&!this.dragged&&event.detail<2)this.toggle(node);});
    group.addEventListener("dblclick", () => {if(!node.group)this.handlers.open(node.path);});
    group.addEventListener("keydown", event => { if(node.group&&(event.key==="Enter"||event.key===" ")){event.preventDefault();this.toggle(node);return;} if (event.key === "Enter") { event.preventDefault(); this.handlers.open(node.path); } if (event.key === " ") { event.preventDefault(); this.choose(node); } });
    if (this.mode === "workflow" && this.project.graph.available) this.ports(group, node);
    if (this.mode === "architecture" && this.project.ownership?.available) this.ownershipPorts(group,node);
    return group;
  }

  /** Keep expansion scoped to the project and restore keyboard focus after the SVG rebuild. */
  toggle(node) {
    if(this.expanded.has(node.id)) this.expanded.delete(node.id); else this.expanded.add(node.id);
    preference(`expanded:${this.project.id}`,[...this.expanded]);
    this.render(this.project,this.mode);
    const current=this.nodes.find(item=>item.id===node.id);
    this.choose(current);
    [...this.cards.children].find(card=>card.getAttribute("data-node")===node.id)?.focus({preventScroll:true});
  }

  /** Ownership ports move capabilities into agent scopes; category links remain structural. */
  ownershipPorts(group,node) {
    if(node.resource) {
      const output=svg("circle",{class:"node-port",cx:WIDTH,cy:HEIGHT/2,r:7,tabindex:0,role:"button","aria-label":`Move ${node.resource.name} to an agent`});
      output.addEventListener("pointerdown",event=>this.connectStart(event,node));
      output.addEventListener("click",event=>{event.stopPropagation();this.setPending(node.id);this.handlers.hint("Choose the receiving agent’s input port, or select its connection to edit ownership.");});
      output.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();event.stopPropagation();this.setPending(node.id);this.handlers.hint("Choose the receiving agent’s input port.");}});
      group.append(output);
    }
    if(node.ownerId) {
      const input=svg("circle",{class:"node-port",cx:0,cy:HEIGHT/2,r:7,tabindex:0,role:"button","data-input":node.ownerId,"aria-label":`Assign capability to ${node.title}`});
      input.addEventListener("pointerdown",event=>event.stopPropagation());
      input.addEventListener("click",event=>{event.stopPropagation();this.finishConnection(node.ownerId);});
      input.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();event.stopPropagation();this.finishConnection(node.ownerId);}});
      group.append(input);
    }
  }

  /** Ports support both dragging and click-to-connect for keyboard and touch users. */
  ports(group, node) {
    const output = svg("circle", {class:"node-port",cx:WIDTH,cy:HEIGHT/2,r:6,tabindex:0,role:"button","aria-label":`Connect from ${node.id}`});
    output.addEventListener("pointerdown", event => this.connectStart(event,node));
    output.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); this.setPending(node.id); this.handlers.hint(`Choose an input port to connect from ${node.id}.`); } });
    group.append(output);
    if (node.id === "START") return;
    const input = svg("circle", {class:"node-port",cx:0,cy:HEIGHT/2,r:6,tabindex:0,role:"button","data-input":node.id,"aria-label":`Connect to ${node.id}`});
    input.addEventListener("pointerdown", event => { event.stopPropagation(); });
    input.addEventListener("click", () => this.finishConnection(node.id));
    input.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); this.finishConnection(node.id); } });
    group.append(input);
  }

  /** Draw a draft connection until an actual destination port is selected. */
  connectStart(event, node) {
    event.preventDefault(); event.stopPropagation(); this.setPending(node.id);
    const draft = svg("path", {class:"edge-draft"}); this.root.append(draft);
    const target = event.currentTarget; target.setPointerCapture(event.pointerId);
    const move = value => { const point = this.point(value); draft.setAttribute("d", this.curve({x:node.x+WIDTH,y:node.y+HEIGHT/2},point)); };
    const finish = value => {
      target.removeEventListener("pointermove",move); target.removeEventListener("pointerup",finish); target.removeEventListener("pointercancel",cancel); draft.remove();
      const destination = document.elementFromPoint(value.clientX,value.clientY)?.closest("[data-input]")?.getAttribute("data-input");
      if (destination) this.finishConnection(destination); else this.handlers.hint(`Choose an input port to connect from ${node.id}.`);
    };
    const cancel = () => { draft.remove(); this.setPending(null); target.removeEventListener("pointermove",move); target.removeEventListener("pointerup",finish); target.removeEventListener("pointercancel",cancel); };
    target.addEventListener("pointermove",move); target.addEventListener("pointerup",finish); target.addEventListener("pointercancel",cancel);
  }

  /** Reveal receiving ports while a connection is pending, including keyboard-only use. */
  setPending(value) {
    this.pending=value;
    this.root?.classList.toggle("connecting",Boolean(value));
  }

  /** Hand topology changes to the revision-checked server instead of mutating only the picture. */
  finishConnection(target) {
    if (!this.pending) return;
    const source = this.pending; this.setPending(null); if(this.mode==="architecture") this.handlers.assign(source,target); else this.handlers.connect(source,target);
  }

  /** Update selection independently from layout so dragging does not open the source editor. */
  choose(node) {
    for (const card of this.cards.children) card.classList.toggle("selected",card.getAttribute("data-node") === node.id);
    this.handlers.select(node);
  }

  /** Persist dragged node positions without mixing visual layout into the agent's Python. */
  drag(event, node, group) {
    if (event.button !== 0) return;
    event.stopPropagation(); this.dragged=false;
    if(!node.group)this.choose(node);
    const origin = this.point(event), start = {x:node.x,y:node.y};
    group.setPointerCapture(event.pointerId);
    const move = value => { if(Math.hypot(value.clientX-event.clientX,value.clientY-event.clientY)<4&&!this.dragged)return;this.dragged=true;const point = this.point(value); node.x = start.x+point.x-origin.x; node.y=start.y+point.y-origin.y; group.setAttribute("transform",`translate(${node.x},${node.y})`); this.drawEdges(); };
    const end = value => {
      group.removeEventListener("pointermove",move); group.removeEventListener("pointerup",end); group.removeEventListener("pointercancel",end);
      if(!this.dragged) return;
      this.positions[node.id]={x:node.x,y:node.y};preference(this.layoutKey,this.positions);
      if(node.resource && value.type!=="pointercancel") {
        const point=this.point(value);
        const owner=this.nodes.find(item=>item.ownerId && item.ownerId!==node.resource.owner && point.x>=item.x && point.x<=item.x+WIDTH && point.y>=item.y && point.y<=item.y+HEIGHT);
        if(owner) this.handlers.assign(node.resource.path,owner.ownerId);
      }
    };
    group.addEventListener("pointermove",move); group.addEventListener("pointerup",end); group.addEventListener("pointercancel",end);
  }

  /** Empty-canvas drags pan the same viewBox used for port hit testing. */
  pan(event) {
    if (event.target !== this.root || event.button !== 0) return;
    const start={x:event.clientX,y:event.clientY,vx:this.view.x,vy:this.view.y};
    this.root.setPointerCapture(event.pointerId);
    const move = value => { this.view.x=start.vx-(value.clientX-start.x)/this.zoom; this.view.y=start.vy-(value.clientY-start.y)/this.zoom; this.viewport(); };
    const end = () => { this.root.removeEventListener("pointermove",move); this.root.removeEventListener("pointerup",end); this.root.removeEventListener("pointercancel",end); };
    this.root.addEventListener("pointermove",move); this.root.addEventListener("pointerup",end); this.root.addEventListener("pointercancel",end);
  }

  /** Route edges consistently after node movement and viewport changes. */
  drawEdges() {
    this.lines.replaceChildren();
    for (const edge of this.edges) {
      const source=this.nodes.find(n=>n.id===edge.source), target=this.nodes.find(n=>n.id===edge.target);
      if (!source || !target) continue;
      const path=svg("path",{class:`edge ${this.mode==="workflow"?"workflow-edge":"ownership-edge"}`,d:this.curve({x:source.x+WIDTH,y:source.y+HEIGHT/2},{x:target.x,y:target.y+HEIGHT/2}),"marker-end":"url(#arrow)"});
      this.lines.append(this.edgeControl(path,edge,source,target));
      if(edge.route) this.lines.append(svg("text",{class:"edge-label",x:(source.x+WIDTH+target.x)/2,y:(source.y+target.y)/2+HEIGHT/2-9},short(edge.route,25)));
    }
  }

  /** Reconnect the nearest end through the curve itself, without extra visible handles. */
  edgeControl(path,edge,source,target) {
    const label=edge.resource?`Change owner of ${edge.resource.name}`:`Edit connection ${source.title} to ${target.title}`;
    const group=svg("g",{class:"editable-edge",tabindex:0,role:"button","aria-label":label});
    const hit=svg("path",{d:path.getAttribute("d"),class:"edge-hit"});
    group.append(svg("title",{},`${label}. Drag the line or an endpoint to reconnect.`),hit,path);
    const edit=()=>this.mode==="workflow"?this.handlers.edge(edge):this.handlers.reconnect(edge,null,null);
    group.addEventListener("pointerdown",event=>{
      const point=this.point(event);
      const from=Math.hypot(point.x-source.x-WIDTH,point.y-source.y-HEIGHT/2);
      const to=Math.hypot(point.x-target.x,point.y-target.y-HEIGHT/2);
      this.edgeDrag(event,edge,from<to?"source":"target");
    });
    group.addEventListener("click",event=>{event.stopPropagation();if(!this.edgeDragged)edit();});
    group.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();event.stopPropagation();edit();}});
    return group;
  }

  /** Keep the source edge unchanged until a dropped endpoint passes server validation. */
  edgeDrag(event,edge,endpoint) {
    if(event.button!==0)return;
    event.preventDefault();event.stopPropagation();this.edgeDragged=false;
    const control=event.currentTarget, start=this.point(event);
    const active=control.closest(".editable-edge");
    active.classList.add("edge-active");
    control.setPointerCapture(event.pointerId);
    const fixedNode=this.nodes.find(node=>node.id===edge[endpoint==="source"?"target":"source"]);
    const fixed={x:fixedNode.x+(endpoint==="source"?0:WIDTH),y:fixedNode.y+HEIGHT/2};
    const draft=svg("path",{class:"edge-draft"});this.root.append(draft);
    const move=value=>{
      const point=this.point(value);
      if(Math.hypot(point.x-start.x,point.y-start.y)>4/this.zoom)this.edgeDragged=true;
      draft.setAttribute("d",endpoint==="source"?this.curve(point,fixed):this.curve(fixed,point));
      const target=this.nodeAt(point);
      for(const card of this.cards.children)card.classList.toggle("drop-target",card.getAttribute("data-node")===target?.id);
    };
    const cleanup=()=>{draft.remove();active.classList.remove("edge-active");window.removeEventListener("keydown",key);control.removeEventListener("pointermove",move);control.removeEventListener("pointerup",end);control.removeEventListener("pointercancel",cancel);for(const card of this.cards.children)card.classList.remove("drop-target");};
    const end=value=>{
      cleanup();if(!this.edgeDragged)return;
      const target=this.nodeAt(this.point(value));
      if(target)this.handlers.reconnect(edge,endpoint,target);
      else this.handlers.hint("Connection unchanged. Drop the endpoint onto a node to validate a connection.");
    };
    const cancel=()=>{cleanup();this.edgeDragged=false;this.handlers.hint("Connection unchanged.");};
    const key=value=>{if(value.key==="Escape"){value.preventDefault();cancel();}};
    window.addEventListener("keydown",key);
    control.addEventListener("pointermove",move);control.addEventListener("pointerup",end);control.addEventListener("pointercancel",cancel);
  }

  /** Test node geometry in canvas coordinates, including zoom and overlapping cards. */
  nodeAt(point) {
    return [...this.nodes].reverse().find(node=>point.x>=node.x-12&&point.x<=node.x+WIDTH+12&&point.y>=node.y&&point.y<=node.y+HEIGHT);
  }

  /** Use a cubic curve that remains readable for forward edges and cycles. */
  curve(start,end) { const bend=curveBend(start,end); return `M${start.x},${start.y} C${start.x+bend},${start.y} ${end.x-bend},${end.y} ${end.x},${end.y}`; }

  /** Translate pointer coordinates through the real SVG transform for accurate zoomed drops. */
  point(event) { if(!this.root) return {x:0,y:0}; return new DOMPoint(event.clientX,event.clientY).matrixTransform(this.root.getScreenCTM().inverse()); }

  /** Keep the viewport's coordinate scale consistent with the zoom control. */
  viewport() {
    if(!this.root) return;
    this.view.width=this.host.clientWidth/this.zoom; this.view.height=this.host.clientHeight/this.zoom;
    this.root.setAttribute("viewBox",`${this.view.x} ${this.view.y} ${this.view.width} ${this.view.height}`);
    this.handlers.zoom(Math.round(this.zoom*100));
  }

  /** Restore automatic spacing only when explicitly requested; retain expanded branches. */
  arrange() {
    preference(this.layoutKey,{});
    this.render(this.project,this.mode);
    this.fit();
  }

  /** Fit the complete authored graph, including nodes moved outside their initial columns. */
  fit() {
    if(!this.nodes.length) return;
    const minX=Math.min(...this.nodes.map(n=>n.x))-40,minY=Math.min(...this.nodes.map(n=>n.y))-45;
    const maxX=Math.max(...this.nodes.map(n=>n.x))+WIDTH+40,maxY=Math.max(...this.nodes.map(n=>n.y))+HEIGHT+65;
    this.zoom=Math.min(1.1,this.host.clientWidth/(maxX-minX),this.host.clientHeight/(maxY-minY));
    this.view.x=(minX+maxX-this.host.clientWidth/this.zoom)/2;
    this.view.y=(minY+maxY-this.host.clientHeight/this.zoom)/2;this.viewport();
  }

  /** Zoom about the visible center instead of snapping back to the origin. */
  scale(factor) {
    const center={x:this.view.x+this.view.width/2,y:this.view.y+this.view.height/2};
    this.zoom=Math.min(2,Math.max(.12,this.zoom*factor));this.viewport();
    this.view.x=center.x-this.view.width/2;this.view.y=center.y-this.view.height/2;this.viewport();
  }
}
