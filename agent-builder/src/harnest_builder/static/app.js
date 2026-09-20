import {$, el, on, error, status, button, field, select, modal, preference, sendDraft, renderDiff, renderCommandOutput, commandOutputText, copyCommandText} from "./ui.js";
import {showDeployment} from "./deployment.js";
import {Canvas, ICONS, kindOf} from "./canvas.js";

const state = {workspace:null, project:null, document:null, dirty:false, library:"components", view:"canvas", context:new Set(), jobs:[], jobId:null, observed:new Set(), callbacks:new Map(), projectRequest:0, fileRequest:0, inspectorRequest:0, prompting:false};
const CLI_KINDS = new Set(["tool","subagent","task","lifecycle","context","mcp","extension"]);
const canvas = new Canvas($("canvas-host"), {drop:(kind,point)=>addComponent(kind,point).catch(error), open:path=>openFile(path).catch(error), select:node=>inspect(node).catch(error), connect:(source,target)=>editConnection({source,target}), edge:edge=>editConnection(edge), assign:(resource,owner)=>editOwnership(resource,owner).catch(error), reconnect:(edge,endpoint,target)=>reconnectEdge(edge,endpoint,target).catch(error), hint:status, zoom:value=>{$("zoom-label").textContent=value+"%";}});
let token = "";
let pollTimer;
let connecting = false;
let connected = false;

/** Read legacy tab credentials without depending on browser storage availability. */
function storedToken(value) {
  try {
    if(value===undefined) return sessionStorage.getItem("harnest-builder-token") || "";
    if(value) sessionStorage.setItem("harnest-builder-token",value);
    else sessionStorage.removeItem("harnest-builder-token");
  } catch { /* The server cookie also supports browsers with unavailable tab storage. */ }
  return "";
}

/** Consume launch links even when navigation changes only the current tab's fragment. */
function authorize() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  token = fragment.get("token") || storedToken();
  if(token) storedToken(token);
  if(fragment.has("token")) history.replaceState(null,"",location.pathname+location.search);
}

/** Keep workspace actions unavailable until the authenticated workspace has loaded. */
function connectionState(ready, message="") {
  connected=ready;
  for(const id of ["new-project","welcome-create","open-project","project-select","mobile-project-select","refresh"]) $(id).disabled=!ready;
  $("connection-panel").hidden=ready;
  $("connection-message").textContent=message || "Connecting to your local workspace…";
  $("connection-form").hidden=ready || !message;
  $("connection-label").textContent=ready?"Local workspace":message?"Disconnected":"Connecting";
  $("welcome").hidden=!ready || Boolean(state.project);
}

/** Retry startup in place; never bind duplicate handlers or start duplicate polling loops. */
async function connect() {
  if(connecting) return;
  connecting=true;clearTimeout(pollTimer);connectionState(false);
  try { await start(); }
  catch(problem) {connectionState(false,problem.message);status(problem.message);}
  finally {connecting=false;}
}

/** Accept only this builder's launch URL, without navigating to a pasted destination. */
async function reconnect(event) {
  event.preventDefault();
  const value=$("launch-url").value.trim();
  if(value) {
    const url=new URL(value);
    if(url.origin!==location.origin) throw new Error("Paste the launch URL for this Studio address.");
    const credential=new URLSearchParams(url.hash.slice(1)).get("token");
    if(!credential) throw new Error("The launch URL must include its #token value.");
    token=credential;storedToken(token);$("launch-url").value="";
  }
  await connect();
}

/** Parse errors from real backend operations; never replace failed requests with demo state. */
async function api(path, method="GET", body) {
  const response=await fetch("/api/"+path,{method,headers:{...(token?{Authorization:"Bearer "+token}:{}),...(body?{"Content-Type":"application/json"}:{})},...(body?{body:JSON.stringify(body)}:{})});
  const payload=await response.json();
  if(!response.ok) {
    const detail=Array.isArray(payload.detail)?payload.detail.map(v=>v.msg).join("; "):payload.detail;
    const problem=new Error(detail || `Request failed (${response.status}).`);
    problem.status=response.status;
    if(response.status===401) {token="";storedToken("");connectionState(false,problem.message);}
    throw problem;
  }
  return payload;
}

/** Load independent app state, then restore a project only if it still exists on disk. */
async function start() {
  await refreshWorkspace();
  token="";storedToken(""); // Subsequent requests use the HttpOnly browser session.
  const saved=preference("project"), projects=state.workspace.projects;
  let selected=projects.find(p=>p.id===saved);
  if(!selected && saved?.startsWith("@") && preference("last-folder")) {
    try {const opened=await api("project/open","POST",{path:preference("last-folder")});await refreshWorkspace();selected=state.workspace.projects.find(p=>p.id===opened.project);}
    catch(problem){error(problem);}
  }
  selected ||= projects[0];
  if(selected) await openProject(selected.id);
  else { renderLibrary(); status("Create an agent to start building."); }
  connectionState(true);
  pollJobs();
}

/** Refresh project discovery without resetting unsaved editor content or selected context. */
async function refreshWorkspace() {
  state.workspace=await api("workspace");
  $("workspace-name").textContent=state.workspace.name;
  const picker=$("project-select"), selected=state.project?.id || picker.value;
  picker.replaceChildren();
  if(!state.workspace.projects.length) { const option=el("option","No projects yet"); option.value=""; picker.append(option); }
  for(const project of state.workspace.projects) { const option=el("option",project.name); option.value=project.id; picker.append(option); }
  picker.value=selected;
  $("mobile-project-select").replaceChildren(...[...picker.options].map(option=>option.cloneNode(true)));
  $("mobile-project-select").value=selected;
  if(!$("model").value) $("model").value=preference("model") || state.workspace.llm.model;
}

/** Prevent project or file navigation from silently discarding an in-progress edit. */
function canLeave() { return !state.dirty || window.confirm("This file has unsaved edits. Discard those edits and continue?"); }

/** Apply only the latest requested project snapshot when users switch quickly. */
async function openProject(identity, force=false) {
  if(!identity) return;
  if(!force && !canLeave()) { for(const id of ["project-select","mobile-project-select"]) $(id).value=state.project?.id||""; return; }
  const ticket=++state.projectRequest;
  const project=await api("project?project="+encodeURIComponent(identity));
  if(ticket!==state.projectRequest) return;
  const changed=state.project?.id!==identity;
  state.project=project; preference("project",identity); preference("last-folder",project.path); $("project-select").value=identity;$("mobile-project-select").value=identity;
  if(changed) { state.fileRequest++; state.inspectorRequest++; state.document=null; state.context=new Set(["agent.py","instructions.md","config.yaml","pyproject.toml","harnest-deployment.yaml"].filter(p=>project.files.includes(p))); resetEditor(); setView("canvas"); }
  $("project-title").textContent=project.config.name;
  $("workspace-name").textContent=project.path;$("workspace-name").title=project.path;
  $("framework-tag").textContent=project.config.framework.toUpperCase(); $("framework-tag").hidden=false;
  $("project-description").textContent=`${project.config.framework} · ${project.config.mode} · ${project.files.length} files`;
  for(const id of ["compile","run-menu","new-file","send-prompt","extensions","deployment","deleted-capabilities"]) $(id).disabled=false;
  $("welcome").hidden=true; $("canvas-controls").hidden=false; $("canvas-legend").hidden=false;
  const workflowOption=$("canvas-mode").querySelector('option[value="workflow"]'); workflowOption.disabled=!project.graph.available;
  if(!project.graph.available) $("canvas-mode").value="architecture";
  renderLibrary(); renderContext(); renderCanvas();
  status(`Opened ${project.config.name}. Source changes save directly to your workspace.`);
}

/** Refresh canvas and source inventory while retaining unsaved editor buffers. */
async function refreshProject() {
  await refreshWorkspace();
  if(state.project) await openProject(state.project.id,true);
}

/** Make the active canvas's topology controls match what the source parser can edit. */
function renderCanvas() {
  const mode=$("canvas-mode").value;
  canvas.render(state.project,mode);
  $("canvas-legend").querySelector("span").textContent=mode==="workflow"?"Source-backed workflow":"Drag either end of a connection to reconnect";
  $("connect-nodes").hidden=mode!=="workflow" || !state.project?.graph.available;
  $("create-workflow").hidden=!state.project || state.project.graph.available || state.project.config.mode!=="managed";
  if(mode==="workflow") status("Drag between node ports to connect. Select an edge to edit or remove it.");
}

/** Group the full capability catalogue and keep palette drops identical to click actions. */
function renderLibrary() {
  const host=$("library"), search=$("library-search").value.toLowerCase().trim(); host.replaceChildren();
  $("file-count").textContent=state.project?.files.length||0;
  if(state.library==="files") { renderFiles(host,search); return; }
  const groups=new Map();
  for(const item of state.workspace?.catalog||[]) {
    if(!`${item.title} ${item.description}`.toLowerCase().includes(search)) continue;
    if(!groups.has(item.group)) { const group=el("section","","library-group");group.append(el("h3",item.group));host.append(group);groups.set(item.group,group); }
    const card=button("",()=>addComponent(item.kind),"palette-card");
    card.title=item.description; card.setAttribute("aria-label",`Add ${item.title}`); card.draggable=Boolean(state.project);
    card.append(el("span",ICONS[item.kind]||"◇","palette-icon"),el("strong",item.title),el("span","⠿","grip"));
    card.addEventListener("dragstart",event=>{event.dataTransfer.setData("application/harnest-component",item.kind);event.dataTransfer.effectAllowed="copy";});
    groups.get(item.group).append(card);
  }
  if(!host.children.length) host.append(el("p","No matching components.","empty-note"));
}

/** Keep the full native source tree accessible, including configuration and capability metadata. */
function renderFiles(host,search) {
  const files=(state.project?.files||[]).filter(path=>path.toLowerCase().includes(search));
  for(const path of files) {
    const item=button(path,()=>openFile(path),"file-item"); item.title=path;
    item.classList.toggle("active",state.document?.path===path);host.append(item);
  }
  if(!files.length) host.append(el("p",state.project?"No matching source files.":"Create or open a project to see its files.","empty-note"));
}

/** Switch between native file navigation and draggable capability creation. */
function setLibrary(name) {
  state.library=name;
  for(const key of ["components","files"]) { $(key+"-tab").classList.toggle("active",name===key);$(key+"-tab").setAttribute("aria-selected",String(name===key)); }
  $("library-search").value="";$("library-search").placeholder=name==="files"?"Find a file…":"Find a component…";renderLibrary();
}

/** Keep editor buffers alive while viewing the canvas. */
function setView(name) {
  state.view=name;
  for(const key of ["canvas","code"]) { $(key+"-tab").classList.toggle("active",name===key);$(key+"-tab").setAttribute("aria-selected",String(name===key));$(key+"-pane").hidden=name!==key; }
  $("canvas-mode").hidden=name!=="canvas";
  $("create-workflow").hidden=name!=="canvas"||!state.project||state.project.graph.available||state.project.config.mode!=="managed";
  $("connect-nodes").hidden=name!=="canvas"||$("canvas-mode").value!=="workflow"||!state.project?.graph.available;
  if(name==="code") setLibrary("files"); else canvas.viewport();
}

/** Build every field before opening the dialog; submit one immutable initialization draft. */
function createProject(draft={}) {
  if(!connected || !state.workspace) {status("Connect to your workspace before creating an agent.");return;}
  const content=el("div","","dialog-form");
  const name=field(content,"Project name",draft.name||"",{placeholder:"research-assistant",required:true,pattern:"[a-z][a-z0-9-]{0,62}",maxLength:63});
  const directory=field(content,"Save in folder",draft.directory||state.workspace.path,{required:true,hint:"A new folder with the project name will be created here."});
  const browse=el("div");content.append(browse);
  const row=el("div","","dialog-grid");content.append(row);
  const framework=select(row,"Framework",[["adk","Google ADK"],["langgraph","LangGraph"]],draft.framework||"adk");
  const mode=select(row,"Authoring mode",[["managed","Managed · automatic discovery"],["advanced","Advanced · native wiring"]],draft.mode||"managed");
  const profile=select(content,"Starting point",[["minimal","Minimal · just the essentials"],["guided","Guided · folder guides"],["example","Examples · optional sample files"]],draft.profile||"minimal");
  const prompt=field(content,"What should this agent do? (optional)",draft.prompt||"",{multiline:true,rows:3,placeholder:"Research a topic, compare sources, and produce a concise report…",hint:"Your description will be ready in the AI panel after initialization."});
  const snapshot=()=>({name:name.value,framework:framework.value,mode:mode.value,profile:profile.value,prompt:prompt.value,directory:directory.value});
  browse.append(button("Browse folders…",()=>{
    const saved=snapshot();
    return chooseFolder("Choose save location",saved.directory,path=>{createProject({...saved,directory:path});return false;},()=>createProject(saved));
  }));
  const preview=el("pre","","command-preview");content.append(preview);
  const update=()=>{preview.textContent=`harnest init "${directory.value}/${name.value||"my-agent"}" --framework ${framework.value} --mode ${mode.value}${profile.value==="guided"?"":" --"+profile.value}`;};
  content.addEventListener("input",update);content.addEventListener("change",update);update();
  const form=modal("Create an agent","Start a real Harnest project. Choose a foundation; add every capability as you build.","Initialize project",async()=>{
    // Jobs outlive the dialog, so completion must never read mutable form controls.
    const saved=snapshot();
    const {prompt:description,...options}=saved;
    const job=await command({action:"init",...options});
    state.callbacks.set(job.id,async()=>{
      await refreshWorkspace();await openProject(job.project);
      if(description.trim()) {$("prompt").value=description;showSide("assistant");$("prompt").focus();}
    });
  });
  form.append(content);
}

/** Browse the local server filesystem using explicit path selection, including non-project parents. */
async function chooseFolder(title, initial, chosen, back) {
  let path;
  const form=modal(title,"Choose a folder on this computer. You can browse or enter an absolute path.","Choose this folder",async()=>{
    const checked=await api("folders?path="+encodeURIComponent(path.value));
    return await chosen(checked.path);
  });
  path=field(form,"Folder path",initial||state.workspace.path,{required:true});
  const controls=el("div","","folder-controls"), list=el("div","","folder-list"), note=el("p","","small muted");
  let ticket=0;
  const load=async target=>{
    const current=++ticket;
    const result=await api("folders?path="+encodeURIComponent(target));
    if(current!==ticket||!form.isConnected)return;
    path.value=result.path;list.replaceChildren();
    controls.replaceChildren(button("Go",()=>load(path.value)),button("Parent folder",()=>load(result.parent)));
    if(back)controls.append(button("Back",back));
    note.textContent=result.agent?"Harnest agent detected in this folder.":"Select a folder below or choose this location.";
    for(const folder of result.folders)list.append(button(folder.name+(folder.agent?" · Harnest agent":""),()=>load(folder.path),"folder-item"));
    if(!result.folders.length)list.append(el("p","No visible subfolders.","empty-note"));
  };
  controls.append(button("Go",()=>load(path.value)));
  form.append(controls,note,list);
  path.addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();load(path.value).catch(error);}});
  await load(path.value);
}

/** Explicitly open an existing folder without changing the identity of any pending edit or job. */
function openFolder() {
  if(!connected || !state.workspace) {status("Connect to your workspace before opening a folder.");return;}
  if(!canLeave())return;
  chooseFolder("Open an agent folder",preference("last-folder")||state.workspace.path,async path=>{
    const result=await api("project/open","POST",{path});
    preference("last-folder",path);await refreshWorkspace();await openProject(result.project,true);
  }).catch(error);
}

/** Show installed packages with source-backed configuration and CLI-owned lifecycle actions. */
async function extensionPanel() {
  const project=state.project.id;
  const form=modal("Harnest extensions","Install packages, inspect capabilities, and edit each extension’s native configuration.","Done",()=>{},true);
  const actions=el("div","","folder-controls");
  actions.append(button("Install extension…",()=>installExtension(project)),button("Create extension…",()=>addComponent("extension")),button("Sync dependencies",async()=>{await command({action:"sync",project});$("dialog").close();}));
  form.append(actions);
  form.append(el("h3","Installed in this agent"));
  const list=el("div","","extension-list");form.append(list);
  const result=await api("extensions?project="+encodeURIComponent(project));
  if(!form.isConnected)return;
  if(!result.extensions.length)list.append(el("p","No extensions installed yet.","empty-note"));
  for(const item of result.extensions) {
    const card=el("section","","extension-card");card.append(el("h3",item.name+" "+item.version),el("p",item.error||item.capabilities.join(" · ")||"No capabilities declared","small muted"));
    const source=select(card,"Extension source",item.files.map(path=>[path,path.split("/").slice(2).join("/ ")]));
    card.append(button("Configure "+item.name,async()=>{if(!canLeave())return;$("dialog").close();await openFile(item.manifest,true);}));
    card.append(button("Edit selected source",async()=>{if(!canLeave())return;$("dialog").close();await openFile(source.value,true);}));
    list.append(card);
  }
  form.append(el("h3","Discover on PyPI"));
  const query=field(form,"Search extensions","",{placeholder:"postgres, docker, slack…"});
  const results=el("div","","extension-list");
  form.append(button("Search catalog",async()=>{
    const job=await command({action:"search-extensions",project,input:query.value});
    results.replaceChildren(el("p","Searching verified Harnest packages. Progress and errors appear in the terminal.","empty-note"));
    const complete=completed=>{if(results.isConnected)renderExtensionSearch(results,completed,project);};
    complete.failed=()=>{if(results.isConnected)results.replaceChildren(el("p","Search failed or was stopped. Close this panel to see the command output, then retry.","empty-note"));};
    state.callbacks.set(job.id,complete);
  }),results);
}

/** Render only inert CLI metadata; installing remains an explicit separate user action. */
function renderExtensionSearch(host,job,project) {
  const items=JSON.parse(job.output.slice(job.output.indexOf("[")))||[];
  host.replaceChildren();
  if(!items.length)host.append(el("p","No compatible extensions found.","empty-note"));
  for(const item of items){const card=el("section","","extension-card");card.append(el("h3",item.name+" "+(item.version||"")),el("p",item.description||"","small muted"),el("span",item.trust,"tag"),button("Install "+item.name,()=>installExtension(project,item.name)));host.append(card);}
}

/** Accept a package slug or a selected local directory and preserve the CLI's no-overwrite policy. */
function installExtension(project,initial="") {
  let source;
  const form=modal("Install a Harnest extension","Choose a PyPI package or local extension folder. Existing extensions are never overwritten. Sync dependencies after installation.","Install extension",async()=>{
    const job=await command({action:"install-extension",project,name:source.value});
    state.callbacks.set(job.id,async()=>{if(state.project?.id===project)await refreshProject();status("Extension installed. Open Extensions to configure it and sync dependencies.");});
  });
  source=field(form,"Package name or absolute folder path",initial,{required:true,placeholder:"harnest-extension-docker"});
  form.append(button("Browse local extension…",()=>chooseFolder("Choose an extension folder",source.value.startsWith("/")?source.value:state.workspace.path,path=>{installExtension(project,path);return false;},()=>installExtension(project,source.value))));
}

/** Open existing root contracts or create components using the authoritative CLI/source route. */
async function addComponent(kind,point) {
  if(!state.project) {createProject();return;}
  const item=state.workspace.catalog.find(c=>c.kind===kind);
  if(!item) return;
  if(item.path) {await openFile(item.path);return;}
  if(kind==="source") {newFile();return;}
  if(kind==="node" && !state.project.graph.available) {convertWorkflow();return;}
  const identity=state.project.id;
  let name,url,tokenEnv,task,schedule;
  const form=modal(`Add ${item.title.toLowerCase()}`,item.description,"Add component",async()=>{
    if(CLI_KINDS.has(kind)) {
      const job=await command({action:"add",project:identity,kind,name:name.value,url:url?.value||"",token_env:tokenEnv?.value||""});
      state.callbacks.set(job.id,async()=>{placeComponent(identity,kind,name.value,point);if(state.project?.id===identity)await refreshProject();});
    } else {
      await api("component","POST",{project:identity,kind,name:name.value,options:{...(task?{task:task.value,schedule:schedule.value}:{})}});
      placeComponent(identity,kind,name.value,point);
      await refreshProject();status(`Created ${item.title.toLowerCase()}. Edit its source to customize it.`);
    }
  });
  name=field(form,"Name","",{required:true,pattern:"[a-z][a-z0-9_-]{0,62}",maxLength:63,placeholder:kind==="tool"?"search":"my_"+kind});
  if(kind==="mcp") {url=field(form,"MCP server URL","",{type:"url",required:true,placeholder:"https://your-server.example/mcp"});tokenEnv=field(form,"Token environment variable (optional)","",{placeholder:"MCP_API_TOKEN",hint:"Stores only the environment variable reference, never the secret."});}
  if(kind==="cron") {
    const tasks=state.project.files.filter(p=>/^tasks\/[^_][^/]*\.py$/.test(p)).map(p=>[p.slice(6,-3),p.slice(6,-3)]);
    task=select(form,"Durable task",tasks.length?tasks:[["","Add a durable task first"]]);task.required=true;
    schedule=field(form,"UTC schedule","0 9 * * *",{required:true,hint:"Starter arguments use payload='scheduled'; edit them to match your task."});
  }
  const notes={sandbox:"Install the docker extension from Run & test → Install extension, then assign this sandbox name to an Agent. Docker is needed when it executes.",node:"This creates a subagent source file and adds it to Graph.nodes. Connect it in Workflow to include it in execution.",storage:"Only one provider may own each storage role. Replace an existing provider instead of creating duplicate authorities.",eval:"Native conversation evals use ADK. For LangGraph, author pytest evaluations through Source file.",subagent:"The CLI adds discovered subagents to managed ADK agents. For a Graph, use Graph agent instead.",tool:"The CLI validates framework and authoring mode. Advanced agents wire tools explicitly in source."};
  if(notes[kind]) form.append(el("p",notes[kind],"empty-note"));
}

/** Remember drop coordinates only for the created component's visual source identity. */
function placeComponent(project,kind,name,point) {
  if(!point) return;
  const directory={tool:"tools",subagent:"subagents",task:"tasks",lifecycle:"lifecycle",context:"lifecycle",mcp:"mcp",node:"subagents",library:"lib",model:"models",storage:"lifecycle",sandbox:"sandbox",cron:"cron"}[kind];
  const mode=$("canvas-mode").value;
  const path=kind==="node"&&mode==="workflow"?name.replaceAll("-","_"):directory?`${directory}/${name.replaceAll("-","_")}.py`:null;
  if(!path) return;
  const key=`layout:${project}:${mode}${mode==="architecture"?":categories":""}`,positions=preference(key)||{};
  positions[path]={x:point.x-110,y:point.y-52};preference(key,positions);
}

/** Start a workflow from an existing Agent while preserving its model, settings, and instructions. */
function convertWorkflow() {
  if(!state.project) return;
  const project=state.project;
  const form=modal("Create a workflow","Keep your current Agent as the first step, then add and connect more graph agents.","Create workflow",async()=>{
    if(state.dirty) throw new Error("Save your source edits before converting the root agent.");
    await api("graph/convert","POST",{project:project.id,revision:project.graph.revision});
    await refreshProject();$("canvas-mode").value="workflow";renderCanvas();
  });
  form.append(el("p","This wraps the existing root Agent in a Graph with a START → respond connection. Its current instructions become explicit node instructions in agent.py.","empty-note"));
}

/** Create arbitrary native text contracts for supported capabilities beyond the starter palette. */
function newFile() {
  if(!state.project) return;
  let path;
  const project=state.project.id;
  const form=modal("Create a source file","Add Python, YAML, JSON, Markdown, or other text source inside this agent.","Create file",async()=>{
    const text=path.value.endsWith(".json")?"{}\n":"";
    await api("files","PUT",{project,files:[{path:path.value,text,revision:""}]});
    await refreshProject();await openFile(path.value);
  });
  path=field(form,"Relative path","",{required:true,placeholder:"lifecycle/auth.py"});
}

/** Bind the editor to a captured project and ignore late replies after navigation. */
async function openFile(path,force=false) {
  if(!state.project || (!force && !canLeave())) return;
  const project=state.project.id,ticket=++state.fileRequest;
  const document=await api(`file?project=${encodeURIComponent(project)}&path=${encodeURIComponent(path)}`);
  if(ticket!==state.fileRequest || state.project.id!==project) return;
  state.document={...document,project};state.dirty=false;
  $("code-editor").value=document.text;$("code-editor").disabled=false;$("file-path").textContent=path;
  $("file-format").textContent=path.split(".").at(-1).toUpperCase()+" · UTF-8";
  $("reload-file").disabled=false;editorChanged();setView("code");$("code-editor").focus();
}

/** Clear ownership whenever the selected project changes. */
function resetEditor() {
  state.dirty=false;$("code-editor").value="";$("code-editor").disabled=true;$("file-path").textContent="Select a file";$("reload-file").disabled=true;editorChanged();
}

/** Track text changes and line numbers without interpreting user source as markup. */
function editorChanged() {
  state.dirty=Boolean(state.document && $("code-editor").value!==state.document.text);
  $("dirty-label").hidden=!state.dirty;$("save-file").disabled=!state.dirty;
  $("line-numbers").textContent=Array.from({length:$("code-editor").value.split("\n").length},(_,i)=>i+1).join("\n");
  $("line-numbers").scrollTop=$("code-editor").scrollTop;
}

/** Save the reviewed revision; preserve edits typed while the request was in flight. */
async function saveFile() {
  if(!state.document || !state.dirty) return;
  const original=state.document, text=$("code-editor").value;
  $("save-file").disabled=true;
  try {
    const result=await api("files","PUT",{project:original.project,files:[{path:original.path,revision:original.revision,text}]});
    if(state.document===original) state.document={...result.files[0],project:original.project};
    editorChanged();await refreshProject();status(`Saved ${original.path}. Build the agent to validate its behavior.`);
  } finally {editorChanged();}
}

/** Inspect a component without replacing the active unsaved source buffer. */
async function inspect(node) {
  const host=$("inspector-content"), ticket=++state.inspectorRequest;
  showSide("inspector");host.replaceChildren();
  const item=state.workspace.catalog.find(c=>c.kind===node.kind);
  host.append(el("span",(item?.title||node.kind).toUpperCase(),"eyebrow"),el("h2",node.title,"inspector-title"),el("div",node.path,"inspector-path"),el("p",item?.description||"An explicit node in the agent workflow."));
  const resource=node.resource || state.project.ownership?.resources.find(item=>item.path===node.path);
  if(resource || node.resources?.length) host.append(button("Change agent…",()=>editOwnership(node),"button secondary"));
  if(!node.id.startsWith("category:") && node.path.includes("/")) host.append(button("Delete capability…",()=>deleteCapability(node),"button secondary"));
  if(node.ownerId && node.group) host.append(button("Edit agent source ↗",()=>openFile(node.path),"button primary"));
  if(node.group) {
    host.append(el("p",`${node.files.length} source file${node.files.length===1?"":"s"}. Expand categories on the canvas or open a file below.`));
    for(const path of node.files) host.append(button(path,()=>openFile(path),"file-item"));
    return;
  }
  host.append(button("Edit source ↗",()=>openFile(node.path),"button primary"));
  if(node.expression) host.append(el("pre",node.expression));
  else {
    const document=await api(`file?project=${encodeURIComponent(state.project.id)}&path=${encodeURIComponent(node.path)}`);
    if(ticket!==state.inspectorRequest) return;
    host.append(el("pre",document.text.slice(0,6000)+(document.text.length>6000?"\n… Open the editor for the full file.":"")));
  }
}

/** Keep prompt history available while inspecting graph components. */
function showSide(name) {
  for(const key of ["assistant","inspector"]) {$(key+"-tab").classList.toggle("active",name===key);$(key+"-tab").setAttribute("aria-selected",String(name===key));$(key+"-content").hidden=name!==key;}
}

/** Preview the full source package and possible references before archiving a capability. */
async function deleteCapability(node) {
  if(state.dirty) throw new Error("Save your source edits before deleting a capability.");
  const project=state.project.id;
  const preview=await api("capabilities/delete-preview","POST",{project,path:node.path});
  const form=modal("Delete capability",`${preview.path} will be removed from this project. You can restore its source from Deleted capabilities.`,"Delete capability",async()=>{
    if(state.dirty) throw new Error("Save your source edits before deleting a capability.");
    await api("capabilities/delete","POST",{project,path:preview.path,revision:preview.revision});
    if(state.project?.id===project) {
      forgetDeletedSource(preview.path);await refreshProject();showSide("assistant");
      status(`Deleted ${preview.path}. Restore it from Deleted capabilities.`);
    }
  });
  form.append(el("p",`${preview.files.length} file${preview.files.length===1?"":"s"} will be removed:`));
  for(const path of preview.files) form.append(el("div",path,"proposal-path"));
  if(preview.references.length) form.append(el("p",`Possible references in: ${preview.references.join(", ")}. Explicit imports, graph nodes, and other code references are not rewritten. Update them and build the agent to validate the change.`,"empty-note"));
}

/** Remove deleted paths from editor state and model context without touching unrelated buffers. */
function forgetDeletedSource(path) {
  const affected=value=>value===path||value.startsWith(path+"/");
  state.context=new Set([...state.context].filter(value=>!affected(value)));
  if(state.document && affected(state.document.path)) {state.document=null;state.fileRequest++;resetEditor();}
  state.inspectorRequest++;$("inspector-content").replaceChildren();
}

/** Offer durable recovery after refresh or restart, with collision checks owned by the server. */
async function deletedCapabilities() {
  if(!state.project)return;
  const project=state.project.id;
  const result=await api(`capabilities/deleted?project=${encodeURIComponent(project)}`);
  const form=modal("Deleted capabilities","Restore source to its original location. Existing files are never overwritten.","Done",async()=>{});
  if(!result.items.length)form.append(el("p","No deleted capabilities in this project.","empty-note"));
  for(const item of result.items) {
    const row=el("div","","dialog-field"), failure=el("p","","dialog-error");
    const restore=button(`Restore ${item.path}`,async()=>{
      if(state.dirty)throw new Error("Save your source edits before restoring a capability.");
      restore.disabled=true;failure.textContent="";
      try {await api("capabilities/restore","POST",{project,identity:item.identity});row.remove();if(state.project?.id===project)await refreshProject();status(`Restored ${item.path}.`);}
      catch(problem){failure.textContent=problem.message;restore.disabled=false;}
    },"button secondary");
    row.append(el("div",`${item.files.length} file${item.files.length===1?"":"s"} · ${new Date(item.deleted_at).toLocaleString()}`),restore,failure);form.append(row);
  }
}

/** Validate the destination of any dragged edge without changing its source optimistically. */
async function reconnectEdge(edge,endpoint,target) {
  if(canvas.mode==="workflow") {
    if(!target) {editConnection(edge);return;}
    const next={...edge,[endpoint]:target.id};delete next.index;
    const edges=state.project.graph.edges.map((item,index)=>index===edge.index?next:item);
    await saveEdges(state.project,edges);return;
  }
  const subject=canvas.nodes.find(node=>node.id===(edge.target==="agent.py"?edge.source:edge.target));
  await editOwnership(subject,target?.ownerId||target?.id);
}

/** Remap both exact files and descendants of complete package moves. */
function movedPath(path,moves) {
  if(!path)return path;
  for(const move of moves) {
    if(path===move.from)return move.to;
    if(path.startsWith(move.from+"/"))return move.to+path.slice(move.from.length);
  }
  return path;
}

/** Keep source selection, context and expansion consistent after a file or package move. */
async function finishOwnership(project,result,paths,owner) {
  const layoutKey=`layout:${project.id}:architecture:categories`, positions=preference(layoutKey)||{};
  for(const move of result.moves) {delete positions[move.from];delete positions[move.to];}
  preference(layoutKey,positions);
  state.context=new Set([...state.context].map(path=>movedPath(path,result.moves)));
  const documentPath=movedPath(state.document?.path,result.moves);
  const expanded=new Set(preference(`expanded:${project.id}`)||[]);
  const agentPath=movedPath(owner,result.moves);
  for(const agent of project.ownership.agents) {
    const path=movedPath(agent.id,result.moves);
    expanded.add(path);expanded.add(`category:subagent${path==="agent.py"?"":":"+path}`);
  }
  for(const kind of ["tool","mcp","skill","subagent"])expanded.add(`category:${kind}${agentPath==="agent.py"?"":":"+agentPath}`);
  for(const path of paths)expanded.add("folder:"+movedPath(path,result.moves));
  preference(`expanded:${project.id}`,[...expanded]);
  await refreshProject();
  const path=movedPath(paths[0],result.moves);
  const selected=canvas.nodes.find(node=>node.path.replace(/\/$/,"")===path);
  if(selected)canvas.choose(selected);
  if(documentPath && documentPath!==state.document?.path)await openFile(documentPath,true);
  status("Connection saved. Source ownership now matches the canvas.");
}

/** Preview all moves through server validation, including category batches and skill assets. */
async function editOwnership(subject,suggestedOwner) {
  const project=state.project, ownership=project?.ownership;
  if(!ownership?.available)throw new Error(ownership?.reason||"Refresh the project before reconnecting.");
  const node=typeof subject==="string"?canvas.nodes.find(item=>item.id===subject||item.path===subject):subject;
  const selected=node?.resources || ownership.resources.filter(item=>item.path===subject);
  const paths=selected.length?selected.map(item=>item.path):[node?.path||String(subject)];
  const content=el("div","","dialog-form");
  const owner=select(content,"Assign to agent",ownership.agents.map(agent=>[agent.id,agent.name]),suggestedOwner||selected[0]?.owner||"agent.py");
  const preview=el("pre","","command-preview");content.append(preview);
  const body=destination=>({project:project.id,resources:paths,owner:destination,revision:ownership.revision});
  const render=result=>{preview.textContent=result.moves.map(move=>`${move.from}\n→ ${move.to}`).join("\n\n")+result.created.map(path=>`\nCreate ${path}`).join("");};
  // A dropped invalid target is rejected before any dialog or file mutation.
  if(suggestedOwner)render(await api("ownership/preview","POST",body(suggestedOwner)));
  else preview.textContent="Choose a receiving agent to validate this connection.";
  let request=0;
  owner.addEventListener("change",async()=>{
    const current=++request;
    try {const result=await api("ownership/preview","POST",body(owner.value));if(current===request)render(result);}
    catch(problem){if(current===request)preview.textContent=problem.message;}
  });
  const form=modal("Reconnect capability", "Move the selected capability or category into an agent’s native scope. Skill packages and subagent branches move with their supporting files.", "Save connection", async()=>{
    if(state.dirty)throw new Error("Save or reload your unsaved source changes before reconnecting.");
    const result=await api("ownership","PUT",body(owner.value));
    await finishOwnership(project,result,paths,owner.value);
  });
  form.append(content);
}

/** Review route changes before writing actual source; deletion removes only the selected edge. */
function editConnection(edge={}) {
  if(!state.project?.graph.available) return;
  const project=state.project, graph=project.graph;
  let source,target,route;
  const form=modal(edge.index===undefined?"Connect workflow nodes":"Edit connection","Connections are saved as Edge declarations in agent.py.","Save connection",async()=>{
    const next={source:source.value,target:target.value,route:route.value||null};
    const edges=graph.edges.filter((_,i)=>i!==edge.index);edges.push(next);
    await saveEdges(project,edges);
  });
  const nodes=graph.nodes.map(n=>[n.id,n.id]);
  source=select(form,"From",[["START","START · entry point"],...nodes],edge.source||"START");
  target=select(form,"To",nodes,edge.target||nodes[0]?.[0]);
  route=field(form,"Conditional route (optional)",edge.route||"",{placeholder:"Leave empty for an unconditional edge"});
  if(edge.index!==undefined) form.append(button("Remove connection",async()=>{await saveEdges(project,graph.edges.filter((_,i)=>i!==edge.index));$("dialog").close();},"button secondary danger"));
}

/** Source revisions prevent visual graph edits from overwriting external code changes. */
async function saveEdges(project,edges) {
  if(state.dirty && state.document?.path==="agent.py") throw new Error("Save or reload your unsaved agent.py edits before changing the graph.");
  await api("graph","PUT",{project:project.id,revision:project.graph.revision,edges});
  await refreshProject();status("Workflow saved to agent.py.");
}

/** Launch fixed public CLI operations and reveal their actual output immediately. */
async function command(body) {
  const job=await api("command","POST",{project:state.project?.id||".",...body});
  state.jobId=job.id;state.jobs.push(job);$("terminal").hidden=false;renderJobs();
  status(`Running harnest ${job.argv.slice(1,3).join(" ")}…`);return job;
}

/** Expose the complete local build/test loop and dependency/provider installation. */
function runMenu() {
  let action,input,port,extension;
  const project=state.project.id;
  const form=modal("Run your agent","Commands run against the project on disk. Save your changes first.","Run command",async()=>{
    await command({action:action.value,project,input:input.value,port:Number(port.value),name:extension.value});
  });
  action=select(form,"Action",[["serve","Start development server · reload enabled"],["run","Send a prompt to the agent"],["test","Run unit tests"],["smoke","Run smoke tests"],["eval","Run evaluations"],["sync","Sync dependency environment"],["install-extension","Install a Harnest extension"]]);
  input=field(form,"Prompt for the agent","",{multiline:true,rows:3,placeholder:"What can you help me with?"});
  port=field(form,"Local server port","1907",{type:"number",min:1024,max:65535});
  extension=field(form,"Extension package or slug","docker",{placeholder:"docker, rag, hatchet, or a package name"});
  const hint=el("p","","empty-note");form.append(hint);
  const update=()=>{input.parentElement.hidden=action.value!=="run";input.required=action.value==="run";port.parentElement.hidden=action.value!=="serve";extension.parentElement.hidden=action.value!=="install-extension";hint.textContent=action.value==="serve"?`The server will listen at http://127.0.0.1:${port.value}. Watch command output for readiness.`:action.value==="run"?"Enable spec.interfaces.cli: true in config.yaml. Live calls use the agent's configured model and tools; results appear in the terminal.":"Commands use the project on disk. Results and failures appear in the terminal.";};
  form.addEventListener("change",update);update();
}

/** Open the guided review, deployment progress, and agent access screen. */
async function deploymentMenu() {
  const project=state.project.id;
  const inspection=await api("deployment/inspect?project="+encodeURIComponent(project));
  if(!inspection.existing) {configureDeployment(inspection);return;}
  await showDeployment({project,api,run:command,isDirty:()=>state.dirty,notice:status,
    edit:async()=>{$("dialog").close();await openFile("harnest-deployment.yaml");},
    watch:(job,success,failed)=>{success.failed=failed;state.callbacks.set(job.id,success);}});
}

/** Turn discovered dependencies into a reviewable manifest without applying infrastructure. */
function configureDeployment(inspection) {
  const project=state.project.id;
  let name,image,backend,context,namespace,port,memory,cpus,variables,services,network;
  const form=modal("Configure deployment","Studio detects runtime inputs from your agent source. Choose what to run and what to connect to, then review the generated files.","Generate configuration",async()=>{
    if(state.dirty)throw new Error("Save your source edits before generating deployment configuration.");
    const proposal=await api("deployment/propose","POST",{project,name:name.value,image:image.value,backend:backend.value,context:context.value,namespace:namespace.value,port:Number(port.value),memory:memory.value,cpus:Number(cpus.value),variables:variables.value.split(/[\s,]+/).filter(Boolean),services_yaml:services.value,network_yaml:network.value});
    showSide("assistant");proposalCard(project,proposal);
    status("Deployment files generated. Review and apply the source changes before previewing deployment.");
  },true);
  name=field(form,"Deployment name",inspection.name,{required:true});
  backend=select(form,"Target",[["local","Local · Docker Compose"],["kubernetes","Kubernetes"]],"local");
  image=field(form,"Agent container image",inspection.name+":local",{required:true,hint:"Build this image with the compiled agent, dependencies and any stdio MCP executables. Its entrypoint must start the server on 0.0.0.0 and the port below."});
  context=field(form,"Kubernetes context","",{placeholder:"Your kubeconfig context"});
  namespace=field(form,"Existing namespace","",{placeholder:"Your Kubernetes namespace"});
  port=field(form,"Agent HTTP port","1907",{type:"number",min:1024,max:65535,required:true});
  memory=field(form,"Agent memory",inspection.resources.memory||"512Mi",{required:true,placeholder:"512Mi or 1Gi"});
  cpus=field(form,"Agent CPUs",inspection.resources.cpu||"1",{type:"number",min:0.01,max:128,step:0.01,required:true});
  variables=field(form,"Runtime environment variables",inspection.variables.join("\n"),{multiline:true,rows:5,hint:"One name per line. Values are supplied by the Studio/CLI environment; no credentials are copied into YAML."});
  services=field(form,"Services",inspection.services_yaml,{multiline:true,rows:12,hint:"mode: connect keeps an external endpoint; mode: provision runs an image. provides injects settings into the agent. Delete optional services you do not want."});
  form.append(button("Add Redis service example",()=>{const current=services.value.trim();if(/^  ?cache:/m.test(current)||/^cache:/m.test(current))throw new Error("A cache service is already defined.");services.value=(current==="{}"?"":current+"\n")+"cache:\n  mode: provision\n  type: redis\n  image: redis:7.4\n  ports: {redis: 6379}\n  healthcheck: {command: [redis-cli, ping]}\n  persistence: {mount: /data, size: 5Gi}\n  provides:\n    REDIS_URL: redis://${services.cache.host}:${services.cache.ports.redis}/0\n";}));
  network=field(form,"Agent network settings","{}",{multiline:true,rows:3,hint:"Optional YAML: hosts: {host.docker.internal: host-gateway} for local host access, or hostname-to-IP mappings. dns: [IP] replaces default DNS; omit it to retain service discovery."});
  for(const note of inspection.warnings)form.append(el("p",note,"empty-note"));
  const update=()=>{const kube=backend.value==="kubernetes";context.parentElement.hidden=!kube;namespace.parentElement.hidden=!kube;context.required=kube;namespace.required=kube;};
  backend.addEventListener("change",update);update();
}

/** Poll with backoff on disconnection and process completions exactly once. */
async function pollJobs() {
  let delay=1000;
  try {
    state.jobs=(await api("jobs")).jobs;
    for(const job of state.jobs) {
      if(job.status==="running"||state.observed.has(job.id)) continue;
      state.observed.add(job.id);
      const callback=state.callbacks.get(job.id);state.callbacks.delete(job.id);
      if(job.status==="succeeded") {if(callback) await callback(job);else if(state.project?.id===job.project) await refreshProject();}
      if(job.status!=="succeeded"&&callback?.failed)callback.failed(job);
      if(job.id===state.jobId) status(job.status==="succeeded"?"Harnest command completed successfully.":`Harnest command ${job.status}. Check command output.`);
    }
    renderJobs();
  } catch(problem) {if(problem.status===401)return;delay=4000;$("connection-label").textContent="Reconnecting";status(problem.message);}
  pollTimer=setTimeout(pollJobs,delay);
}

/** Preserve terminal selection and follow output only when the reader is near the end. */
function renderJobs() {
  const selector=$("job-select"), old=state.jobId;
  selector.replaceChildren();
  for(const job of state.jobs) {const option=el("option",`harnest ${job.argv.slice(1,3).map(v=>v.split("/").at(-1)).join(" ")} · ${job.status}`);option.value=job.id;selector.append(option);}
  state.jobId=state.jobs.some(j=>j.id===old)?old:state.jobs.at(-1)?.id;
  if(state.jobId) selector.value=state.jobId;
  const job=state.jobs.find(j=>j.id===state.jobId), output=$("job-output");
  renderCommandOutput(output,job?`$ ${job.argv.join(" ")}\n\n${job.output||"Starting command…"}`:"Run a Harnest command to see output here.",job?.id);
  $("copy-output").disabled=!job;
  $("job-status").textContent=job?.status||"idle";$("stop-job").hidden=job?.status!=="running";
  const active=state.jobs.filter(j=>j.status==="running").length;$("running-count").textContent=active?`· ${active} running`:"";
}

/** List precisely which source files will be shared with the configured model provider. */
function renderContext() {
  const host=$("context-files");host.replaceChildren();
  for(const path of state.project?.files||[]) {
    const label=el("label"), checkbox=el("input");checkbox.type="checkbox";checkbox.checked=state.context.has(path);
    checkbox.addEventListener("change",()=>{if(checkbox.checked)state.context.add(path);else state.context.delete(path);updateContextCount();});
    label.append(checkbox,document.createTextNode(path));host.append(label);
  }
  updateContextCount();
}

/** Keep context selection visible even when the disclosure is collapsed. */
function updateContextCount() {$("context-count").textContent=`${state.context.size} files`;}

/** Append inert conversation text and reveal the latest provider result. */
function message(text,kind="assistant") {
  const node=el("div",text,"message "+kind);$("conversation").append(node);node.scrollIntoView({block:"nearest"});return node;
}

/** Generate a real model proposal, with source sharing and apply kept separate. */
async function sendPrompt(event) {
  event.preventDefault();
  if(!state.project||state.prompting) return;
  if(state.dirty) throw new Error("Save your source edits before sending them to the model.");
  const project=state.project.id,prompt=$("prompt").value.trim(),model=$("model").value.trim();
  if(!prompt) return;
  state.prompting=true;$("send-prompt").disabled=true;showSide("assistant");
  preference("model",model);message(prompt,"user");
  const pending=message("Harnest builder agent is reading source and preparing a code proposal…");pending.classList.add("pending");
  try {
    const proposal=await sendDraft($("prompt"),submitted=>api("propose","POST",{project,prompt:submitted,model,paths:[...state.context],allow_related_source:$("allow-related-source").checked}),()=>state.project?.id===project);
    pending.remove();proposalCard(project,proposal);
  } catch(problem) {pending.classList.remove("pending");pending.classList.add("error");pending.textContent=problem.message;}
  finally {state.prompting=false;$("send-prompt").disabled=!state.project;}
}

/** Retain proposals with their original project identity across navigation. */
function proposalCard(project,proposal) {
  const card=el("section","","proposal-card");
  card.append(el("h3",`✧ ${proposal.files.length} proposed file changes`),el("p",proposal.summary),el("p",`Project: ${project}`));
  if(proposal.context_paths) card.append(el("p",`Source read: ${proposal.context_paths.join(", ")}`));
  for(const file of proposal.files) card.append(el("div",(file.revision?"~ ":"+ ")+file.path,"proposal-path"));
  const review=button("Review changes ↗",()=>reviewProposal(project,proposal,review),"button primary");card.append(review);$("conversation").append(card);card.scrollIntoView({block:"nearest"});
}

/** Review every proposed file against its exact original text before one conflict-checked apply. */
function reviewProposal(project,proposal,reviewButton) {
  const form=modal("Review proposed changes",proposal.summary,"Apply to project",async()=>{
    if(state.dirty) throw new Error("Save your editor changes before applying a proposal.");
    await api("files","PUT",{project,files:proposal.files.map(({path,text,revision})=>({path,text,revision}))});
    reviewButton.textContent="Applied to project ✓";reviewButton.disabled=true;
    if(state.project?.id===project) {await refreshProject();if(state.document) await openFile(state.document.path,true);}
    status("Changes applied. Preview the deployment plan or build and test the agent to validate them.");
  },true);
  const tabs=el("div","","review-tabs"),preview=el("div","","review-diff");form.append(tabs,preview);
  const choose=file=>{renderDiff(preview,file);for(const tab of tabs.children)tab.classList.toggle("active",tab.textContent===file.path);};
  for(const file of proposal.files) tabs.append(button(file.path,()=>choose(file),""));
  choose(proposal.files[0]);
}

/** Explain provider setup without accepting or persisting credentials in browser state. */
function providerHelp() {
  const form=modal("Connect your model","Build with AI runs a compiled Harnest agent grounded in the bundled Harnest authoring skills. It uses your chosen model through LiteLLM. Set provider credentials in the terminal that starts Studio, then restart Studio.","Done",async()=>{});
  form.append(el("pre","export HARNEST_BUILDER_MODEL=provider/model\n\n# Use your provider's standard key environment variable,\n# or a builder-specific key:\nexport HARNEST_BUILDER_API_KEY=...\n\n# Optional compatible endpoint:\nexport HARNEST_BUILDER_API_BASE=https://your-endpoint/v1","command-preview"));
  form.append(el("p","Provider requests include your prompt and selected source files. Credentials are read only by the local server.","empty-note"));
}

/** Bind single-instance controls once; source and job refreshes replace only their own content. */
function bind() {
  bindNavigation();bindEditor();bindJobs();bindAssistant();
  on($("new-project"),"click",()=>createProject());on($("welcome-create"),"click",()=>createProject());
  on($("open-project"),"click",openFolder);on($("extensions"),"click",extensionPanel);
  on($("deployment"),"click",deploymentMenu);
  on($("compile"),"click",()=>command({action:"compile"}));on($("run-menu"),"click",runMenu);
  on($("deleted-capabilities"),"click",deletedCapabilities);
  on($("new-file"),"click",newFile);on($("refresh"),"click",refreshProject);
  on($("project-select"),"change",()=>openProject($("project-select").value));
  on($("mobile-project-select"),"change",()=>openProject($("mobile-project-select").value));
  $("library-search").addEventListener("input",renderLibrary);
  $("arrange-canvas").addEventListener("click",()=>canvas.arrange());$("zoom-in").addEventListener("click",()=>canvas.scale(1.2));$("zoom-out").addEventListener("click",()=>canvas.scale(1/1.2));$("fit-canvas").addEventListener("click",()=>canvas.fit());
  $("canvas-mode").addEventListener("change",renderCanvas);on($("connect-nodes"),"click",()=>editConnection());on($("create-workflow"),"click",convertWorkflow);
}

/** Tabs expose their selection to both the visual styling and assistive technology. */
function bindNavigation() {
  for(const key of ["components","files"]) $(key+"-tab").addEventListener("click",()=>setLibrary(key));
  for(const key of ["canvas","code"]) $(key+"-tab").addEventListener("click",()=>setView(key));
  for(const key of ["assistant","inspector"]) $(key+"-tab").addEventListener("click",()=>showSide(key));
}

/** Keyboard save and indentation operate on real textarea source, preserving browser undo. */
function bindEditor() {
  $("code-editor").addEventListener("input",editorChanged);
  $("code-editor").addEventListener("scroll",()=>{$("line-numbers").scrollTop=$("code-editor").scrollTop;});
  $("code-editor").addEventListener("keydown",event=>{if(event.key==="Tab"){event.preventDefault();const editor=event.target;editor.setRangeText("    ",editor.selectionStart,editor.selectionEnd,"end");editorChanged();}});
  on($("save-file"),"click",saveFile);on($("reload-file"),"click",()=>openFile(state.document.path));
  window.addEventListener("keydown",event=>{if((event.metaKey||event.ctrlKey)&&event.key==="s"){event.preventDefault();saveFile().catch(error);}});
  window.addEventListener("beforeunload",event=>{if(state.dirty){event.preventDefault();event.returnValue="";}});
}

/** Terminal visibility does not affect subprocess lifetime; Stop explicitly cancels ownership. */
function bindJobs() {
  on($("copy-output"),"click",()=>copyCommandText(commandOutputText($("job-output"))));
  $("show-terminal").addEventListener("click",()=>{$("terminal").hidden=!$("terminal").hidden;});
  $("close-terminal").addEventListener("click",()=>{$("terminal").hidden=true;});
  $("job-select").addEventListener("change",()=>{state.jobId=$("job-select").value;renderJobs();});
  on($("stop-job"),"click",async()=>{await api(`jobs/${state.jobId}/stop`,"POST");status("Command stopped.");});
}

/** Suggested prompts populate editable input; they never silently send workspace source. */
function bindAssistant() {
  on($("prompt-form"),"submit",sendPrompt);on($("provider-help"),"click",providerHelp);
  for(const node of document.querySelectorAll("[data-prompt]")) node.addEventListener("click",()=>{$("prompt").value=node.dataset.prompt;$("prompt").focus();});
}

authorize();bind();
on($("connection-form"),"submit",reconnect);
window.addEventListener("hashchange",()=>{authorize();connect();});
connect();
