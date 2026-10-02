import {StudioEditor} from "./editor.js";
import {runBuilder, activityCard} from "./agui.js";
import {AgentExperience, agentName} from "./experience.js";
import {runtimeFields, evaluationFields, executionFields} from "./components.js";
import {mcpPanel,reviewMCP} from "./mcp.js";
import {$, el, on, error, status, button, field, select, modal, preference, sendDraft, renderDiff, renderCommandOutput, commandOutputText, copyCommandText, buildFileTree, renderFileTree} from "./ui.js";
import {renderDeploymentControl} from "./features.js";
import {Canvas, ICONS, kindOf} from "./canvas.js";
import {attachDrawer} from "./drawer.js";
import {commandText, initCommand, jobCommand} from "./commands.js";

/** Mount the default workspace through the public Studio host contract. */
export async function activate(studio, host) {
await studio.mountHTML(host, "index.html");
const lifecycle=new AbortController();
const rightPanel=attachDrawer({panel:$("studio-assistant-panel"),toggle:$("assistant-toggle"),dialog:$("assistant-drawer"),body:$("assistant-drawer-body"),close:$("assistant-drawer-close")});
const state = {workspace:null, project:null, document:null, dirty:false, library:"components", view:"canvas", context:new Set(), jobs:[], jobId:null, observed:new Set(), callbacks:new Map(), projectRequest:0, fileRequest:0, inspectorRequest:0, prompting:false};
const canvas = new Canvas($("canvas-host"), {drop:(kind,point)=>addComponent(kind,point).catch(error), open:path=>openFile(path).catch(error), select:node=>inspect(node).catch(error), connect:(source,target)=>editConnection({source,target}), edge:edge=>editConnection(edge), assign:(resource,owner)=>editOwnership(resource,owner).catch(error), reconnect:(edge,endpoint,target)=>reconnectEdge(edge,endpoint,target).catch(error), hint:status, zoom:value=>{$("zoom-label").textContent=value+"%";}});
let pollTimer;
let connecting = false;
let connected = false;
const sourceEditor=new StudioEditor({host:$("source-editor"),textarea:$("code-editor"),status:$("lint-status"),request:body=>api("diagnostics","POST",body),changed:editorChanged});
const conversations=new Map();
const modViews=new Map();
const collapsedFolders=new Map();
const conversationTemplate=$("conversation").cloneNode(true);
const experience=new AgentExperience({api,project:()=>state.project,dirty:()=>state.dirty,view:setView,
  track:job=>{state.jobs=state.jobs.filter(item=>item.id!==job.id);state.jobs.push(job);state.jobId=job.id;renderJobs();},
  logs:job=>{if(job){state.jobId=job.id;$("terminal").hidden=false;renderJobs();}},
  build:async(project,prompt)=>{await openProject(project);if(state.project?.id!==project)return;setView("canvas");showSide("assistant");$("prompt").value=prompt;$("prompt").focus();}});

/** Store builder drafts and proposals with their project, never with the visible tab. */
function builderConversation(project) {
  if(!conversations.has(project))conversations.set(project,{host:conversationTemplate.cloneNode(true),draft:"",history:[]});
  return conversations.get(project);
}

/** Restore project-specific authoring history without losing asynchronous proposal cards. */
function selectConversation(previous,identity) {
  if(previous)builderConversation(previous).draft=$("prompt").value;
  const conversation=builderConversation(identity);
  $("conversation").replaceWith(conversation.host);$("prompt").value=conversation.draft;
}

/** Keep workspace actions unavailable until the authenticated workspace has loaded. */
function connectionState(ready, message="") {
  connected=ready;studio.state.publish("connection",{ready,message});
  for(const id of ["new-project","open-project","project-select","mobile-project-select","refresh","studio-packs"]) $(id).disabled=!ready;
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

/** Let the host consume replacement launch credentials without exposing them to packs. */
async function reconnect(event) {
  event.preventDefault();
  studio.authorize($("launch-url").value.trim());
  $("launch-url").value="";
  await connect();
}

/** Delegate authenticated transport to the host shared by all trusted UI packs. */
async function api(path, method="GET", body) {
  try {
    const value=await studio.request(path, method, body);
    lifecycle.signal.throwIfAborted();return value;
  } catch(problem) {
    if(!lifecycle.signal.aborted&&problem.status===401)connectionState(false,problem.message);
    throw problem;
  }
}

/** Load independent app state, then restore a project only if it still exists on disk. */
async function start() {
  await refreshWorkspace();
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
  renderDeploymentControl($("deployment"),state.workspace,state.project);
  const picker=$("project-select"), selected=state.project?.id || picker.value;
  picker.replaceChildren();
  if(!state.workspace.projects.length) { const option=el("option","No projects yet"); option.value=""; picker.append(option); }
  for(const project of state.workspace.projects) { const option=el("option",project.label || project.name); option.value=project.id; option.title=project.path; picker.append(option); }
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
  const previous=state.project?.id;
  if(changed)selectConversation(previous,identity);
  state.project=project; studio.state.publish("project",{id:project.id,name:project.config.name,framework:project.config.framework}); preference("project",identity); preference("last-folder",project.path); $("project-select").value=identity;$("mobile-project-select").value=identity;
  if(changed) { state.fileRequest++; state.inspectorRequest++; state.document=null; state.context=new Set(["agent.py","instructions.md","config.yaml","pyproject.toml","harnest-deployment.yaml"].filter(p=>project.files.includes(p))); resetEditor(); setView("canvas"); }
  $("project-title").textContent=project.config.name;
  $("framework-tag").textContent=project.config.framework.toUpperCase(); $("framework-tag").hidden=false;
  $("project-description").textContent=`${project.config.framework} · ${project.config.mode} · ${project.files.length} files`;
  for(const id of ["compile","run-menu","new-file","send-prompt","extensions","mcp-connections","deleted-capabilities","step-build","step-connect","step-run","run-agent","try-tab"]) $(id).disabled=false;
  $("send-prompt").disabled=state.prompting;
  renderDeploymentControl($("deployment"),state.workspace,state.project);
  $("welcome").hidden=true; $("canvas-controls").hidden=false; $("canvas-legend").hidden=false;
  const workflowOption=$("canvas-mode").querySelector('option[value="workflow"]'); workflowOption.disabled=!project.graph.available;
  if(!project.graph.available) $("canvas-mode").value="architecture";
  renderLibrary(); renderContext(); renderCanvas();
  if(changed)experience.select();
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
  renderPackLibrary(host,search);
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

/** Keep folder expansion separate for each project across searches and source refreshes. */
function projectFolders() {
  const identity=state.project?.id;
  if(!collapsedFolders.has(identity))collapsedFolders.set(identity,new Set());
  return collapsedFolders.get(identity);
}

/** Show the source hierarchy while retaining full paths for opening and filtering files. */
function renderFiles(host,search) {
  const nodes=buildFileTree(state.project?.files||[],search);
  renderFileTree(host,nodes,{collapsed:projectFolders(),selected:state.document?.path,search,open:openFile});
  if(!nodes.length)host.append(el("p",state.project?"No matching source files.":"Create or open a project to see its files.","empty-note"));
}

/** Switch between native file navigation and draggable capability creation. */
function setLibrary(name) {
  state.library=name;
  document.querySelector(".library-foot").hidden=name==="files";
  for(const key of ["components","files"]) { $(key+"-tab").classList.toggle("active",name===key);$(key+"-tab").setAttribute("aria-selected",String(name===key)); }
  $("library-search").value="";$("library-search").placeholder=name==="files"?"Find a file…":"Find a component…";renderLibrary();
}

/** Keep editor buffers alive while viewing the canvas. */
function setView(name) {
  state.view=name;
  for(const [id,view] of modViews){view.host.hidden=name!==id;view.tab.classList.toggle("active",name===id);view.tab.setAttribute("aria-selected",String(name===id));}
  document.querySelector('[data-studio-slot="workspace.tabs"]').hidden=!modViews.has(name);
  for(const key of ["canvas","code","try"]) { $(key+"-tab").classList.toggle("active",name===key);$(key+"-tab").setAttribute("aria-selected",String(name===key));$(key+"-pane").hidden=name!==key; }
  $("canvas-mode").hidden=name!=="canvas";
  $("create-workflow").hidden=name!=="canvas"||!state.project||state.project.graph.available||state.project.config.mode!=="managed";
  $("connect-nodes").hidden=name!=="canvas"||$("canvas-mode").value!=="workflow"||!state.project?.graph.available;
  if(name==="code") setLibrary("files");
  if(name==="canvas")canvas.viewport();
  if(name==="try")experience.poll().catch(error);
}

/** Build every field before opening the dialog; submit one immutable initialization draft. */
function createProject(draft={}) {
  if(!connected || !state.workspace) {status("Connect to your workspace before creating an agent.");return;}
  const content=el("div","","dialog-form");
  const prompt=field(content,"What should your agent do?",draft.prompt||"",{multiline:true,rows:4,placeholder:"Help customers track orders and resolve delivery issues…",maxLength:16000,hint:"Studio will prepare source changes for you to review. Leave blank to start from an empty project."});
  const model=field(content,"Builder model",draft.model||$("model").value,{placeholder:"provider/model",hint:"Uses the provider credentials configured on the Studio server."});
  const name=field(content,"Agent name",draft.name||agentName(draft.prompt||""),{placeholder:"research-assistant",required:true,pattern:"[a-z][a-z0-9-]{0,62}",maxLength:63});
  const advanced=el("details","","creation-options"),optionsHost=el("div","","dialog-form");advanced.append(el("summary","Project settings"),optionsHost);content.append(advanced);
  const directory=field(optionsHost,"Save in folder",draft.directory||state.workspace.path,{required:true,hint:"A new folder with the project name will be created here."});
  const browse=el("div");optionsHost.append(browse);
  const row=el("div","","dialog-grid");optionsHost.append(row);
  const framework=select(row,"Framework",[["adk","Google ADK"],["langgraph","LangGraph"]],draft.framework||"adk");
  const mode=select(row,"Authoring mode",[["managed","Managed · automatic discovery"],["advanced","Advanced · native wiring"]],draft.mode||"managed");
  const profile=select(optionsHost,"Starting point",[["minimal","Minimal · just the essentials"],["guided","Guided · folder guides"],["example","Examples · optional sample files"]],draft.profile||"minimal");
  let named=Boolean(draft.name);name.addEventListener("input",()=>{named=true;});prompt.addEventListener("input",()=>{if(!named)name.value=agentName(prompt.value);});
  const snapshot=()=>({name:name.value,framework:framework.value,mode:mode.value,profile:profile.value,prompt:prompt.value,directory:directory.value,model:model.value});
  browse.append(button("Browse folders…",()=>{
    const saved=snapshot();
    return chooseFolder("Choose save location",saved.directory,path=>{createProject({...saved,directory:path});return false;},()=>createProject(saved));
  }));
  const preview=el("pre","","command-preview");optionsHost.append(preview);
  const update=()=>{preview.textContent=initCommand(state.workspace.cli,snapshot());};
  content.addEventListener("input",update);content.addEventListener("change",update);update();
  const form=modal("What will your agent do?","Create an editable Harnest project. Your description and initial source will be sent to the configured builder model to draft changes for review.","Create agent",async()=>{
    // Jobs outlive the dialog, so completion must never read mutable form controls.
    const saved=snapshot();
    const {prompt:description,model:builderModel,...options}=saved;
    if(description.trim() && !builderModel.trim())throw new Error("Choose a builder model before generating your agent, or leave the description blank to start with source.");
    $("model").value=builderModel;preference("model",builderModel);
    const job=await command({action:"init",...options});
    state.callbacks.set(job.id,async()=>{
      await refreshWorkspace();await openProject(job.project);
      if(description.trim() && state.project?.id===job.project) {$("prompt").value=description;showSide("assistant");sendPrompt({preventDefault(){}}).catch(error);}
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
  actions.append(button("Install extension…",()=>installExtension(project)),button("Create extension…",()=>addComponent("extension")),button("Sync dependencies",()=>syncDependencies(project)));
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

/** Review an agent's AG-UI transport policy before applying source changes. */
async function configureTransports() {
  if(state.dirty) throw new Error("Save your source edits before configuring transports.");
  const project=state.project.id, current=await api("transports?project="+encodeURIComponent(project));
  if(state.project?.id!==project)return;
  let choice;
  const form=modal("AG-UI transport",`Configure ${current.path}. AG-UI sends text, tool calls, structured state, results, and resumable actions over SSE. Rebuild and restart after applying changes.`,"Review change",async()=>{
    const proposed=await api("transports/preview","POST",{project,agui:choice.value==="true"});
    proposalCard(project,proposed);showSide("assistant");
  });
  choice=select(form,"AG-UI endpoint",[["true","Enabled · POST /agui"],["false","Disabled"]],String(current.agui!==false));
  if(typeof current.agui==="string")form.append(el("p",`Current value uses ${current.agui}. Applying a choice replaces this environment reference with an explicit boolean.`));
}

/** Open existing root contracts or create components using the authoritative CLI/source route. */
async function addComponent(kind,point) {
  if(!state.project) {createProject();return;}
  const item=state.workspace.catalog.find(c=>c.kind===kind);
  if(!item) return;
  if(item.path) {await openFile(item.path);return;}
  if(kind==="source") {newFile();return;}
  if(kind==="transports") {await configureTransports();return;}
  if(kind==="node" && !state.project.graph.available) {convertWorkflow();return;}
  const identity=state.project.id;
  const metrics=kind==="eval"?(await api("evaluation-metrics")).metrics:[];
  if(state.project?.id!==identity)return;
  let name,url,tokenEnv,via,metric,options=()=>({});
  const form=modal(`Add ${item.title.toLowerCase()}`,item.description,"Add component",async()=>{
    const createdName=name.value;
    if(item.command) {
      const job=await command({action:"add",project:identity,kind,name:createdName,url:url?.value||"",token_env:tokenEnv?.value||"",via:via?.value||"",metric:metric?.value||"response_match_score"});
      state.callbacks.set(job.id,async()=>{placeComponent(identity,kind,createdName,point);if(state.project?.id===identity)await refreshProject();});
    } else {
      await api("component","POST",{project:identity,kind,name:createdName,options:options()});
      placeComponent(identity,kind,createdName,point);
      await refreshProject();status(`Created ${item.title.toLowerCase()}. Edit its source to customize it.`);
    }
  });
  name=field(form,"Name","",{required:true,pattern:"[a-z][a-z0-9_-]{0,62}",maxLength:63,placeholder:kind==="channel"?"slack":kind==="tool"?"search":"my_"+kind});
  if(kind==="mcp") {url=field(form,"MCP server URL","",{type:"url",required:true,placeholder:"https://your-server.example/mcp"});tokenEnv=field(form,"Token environment variable (optional)","",{placeholder:"MCP_API_TOKEN",hint:"Stores only the environment variable reference, never the secret."});}
  if(kind==="channel") via=field(form,"Transport extension","fused",{required:true,hint:"The installed extension owns the platform transport and credentials."});
  options=runtimeFields(kind,form,state.project.files);
  if(kind==="eval")metric=evaluationFields(form,metrics);
  const notes={sandbox:"Install the docker extension from Run & test → Install extension, then assign this sandbox name to an Agent. Docker is needed when it executes.",node:"This creates a subagent source file and adds it to Graph.nodes. Connect it in Workflow to include it in execution.",storage:"Only one provider may own each storage role. Replace an existing provider instead of creating duplicate authorities.",subagent:"The CLI adds discovered subagents to managed ADK agents. For a Graph, use Graph agent instead.",tool:"The CLI validates framework and authoring mode. Advanced agents wire tools explicitly in source."};
  if(notes[kind]) form.append(el("p",notes[kind],"empty-note"));
}

/** Remember drop coordinates only for the created component's visual source identity. */
function placeComponent(project,kind,name,point) {
  if(!point) return;
  const directory={tool:"tools",subagent:"subagents",task:"tasks",lifecycle:"lifecycle",context:"lifecycle",mcp:"mcp",channel:"channels",node:"subagents",library:"lib",model:"models",storage:"lifecycle","task-storage":"lifecycle","client-input":"tools",sandbox:"sandbox",cron:"cron"}[kind];
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
  // Opening a file from the canvas also reveals its location in the Files tree.
  const parents=path.split("/");parents.pop();
  while(parents.length){projectFolders().delete(parents.join("/"));parents.pop();}
  if(state.library==="files")renderLibrary();
  $("reload-file").disabled=false;editorChanged();setView("code");sourceEditor.focus();
}

/** Clear ownership whenever the selected project changes. */
function resetEditor() {
  state.dirty=false;$("code-editor").value="";$("code-editor").disabled=true;$("file-path").textContent="Select a file";$("reload-file").disabled=true;editorChanged();
}

/** Track unsaved source while synchronizing the visible editor and draft diagnostics. */
function editorChanged() {
  state.dirty=Boolean(state.document && $("code-editor").value!==state.document.text);
  $("dirty-label").hidden=!state.dirty;$("save-file").disabled=!state.dirty;
  sourceEditor.sync($("code-editor").value,state.document?.path,state.document?.project,$("code-editor").disabled);
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
  rightPanel.open();
}

/** Preview the full source package and possible references before archiving a capability. */
async function deleteCapability(node) {
  if(state.dirty) throw new Error("Save your source edits before deleting a capability.");
  const project=state.project.id;
  const preview=await api("capabilities/delete-preview","POST",{project,path:node.path});
  const form=modal("Delete capability",`${preview.path} will be removed from this project. You can restore its source from Recently deleted.`,"Delete capability",async()=>{
    if(state.dirty) throw new Error("Save your source edits before deleting a capability.");
    await api("capabilities/delete","POST",{project,path:preview.path,revision:preview.revision});
    if(state.project?.id===project) {
      forgetDeletedSource(preview.path);await refreshProject();showSide("assistant");
      status(`Deleted ${preview.path}. Restore it from Recently deleted.`);
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
  const form=modal("Recently deleted","Tools, skills, and other agent components removed in Studio. Restore their files to the original location without overwriting existing source.","Close",async()=>{},false,{cancel:false});
  if(!result.items.length)form.append(el("p","Nothing to restore. Components you remove in Studio will appear here.","empty-note"));
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
  status(`Running ${jobCommand(job)}…`);return job;
}

/** Keep validation in one place; serving, chat and extensions have dedicated screens. */
function checksMenu() {
  let action,options;
  const project=state.project.id;
  const form=modal("Check your agent","Run tests or evaluations against your saved project. Results appear in the terminal.","Run check",async()=>{
    if(state.dirty)throw new Error("Save your source changes before running checks.");
    await command({action:action.value,project,...options()});
  });
  action=select(form,"Action",[["test","Unit tests"],["smoke","Smoke tests"],["eval","Evaluations"]]);
  options=executionFields(form,action);
}

/** Keep environment maintenance beside extension installation, preserving profile choice. */
function syncDependencies(project) {
  let options;
  const form=modal("Sync dependencies","Update the dependencies for this project.","Sync dependencies",async()=>{
    if(state.dirty)throw new Error("Save your source changes before syncing dependencies.");
    await command({action:"sync",project,...options()});
  });
  options=executionFields(form,{value:"sync"});
}

/** Poll with backoff while mounted and process completions exactly once. */
async function pollJobs() {
  if(lifecycle.signal.aborted)return;
  let delay=1000;
  try {
    state.jobs=(await api("jobs")).jobs;
    studio.state.publish("jobs",state.jobs);
    for(const job of state.jobs) {
      if(job.status==="running"||state.observed.has(job.id)) continue;
      state.observed.add(job.id);
      const callback=state.callbacks.get(job.id);state.callbacks.delete(job.id);
      if(job.status==="succeeded") {if(callback) await callback(job);else if(state.project?.id===job.project) await refreshProject();}
      if(job.status!=="succeeded"&&callback?.failed)callback.failed(job);
      if(job.id===state.jobId) status(job.status==="succeeded"?"Command completed successfully.":`Command ${job.status}. Check command output.`);
    }
    renderJobs();await experience.poll();
  } catch(problem) {if(lifecycle.signal.aborted||problem.status===401)return;delay=4000;$("connection-label").textContent="Reconnecting";status(problem.message);}
  if(!lifecycle.signal.aborted)pollTimer=setTimeout(pollJobs,delay);
}

/** Preserve terminal selection and follow output only when the reader is near the end. */
function renderJobs() {
  const selector=$("job-select"), old=state.jobId;
  selector.replaceChildren();
  for(const job of state.jobs) {const option=el("option",`${jobCommand(job)} · ${job.status}`);option.value=job.id;selector.append(option);}
  state.jobId=state.jobs.some(j=>j.id===old)?old:state.jobs.at(-1)?.id;
  if(state.jobId) selector.value=state.jobId;
  const job=state.jobs.find(j=>j.id===state.jobId), output=$("job-output");
  renderCommandOutput(output,job?`$ ${commandText(job.argv)}\n\n${job.output||"Starting command…"}`:"Run a command to see output here.",job?.id);
  let workLink=$("open-runtime-work");
  if(!workLink) {workLink=el("a","Tasks & schedules ↗","button secondary");workLink.id="open-runtime-work";workLink.target="_blank";workLink.rel="noopener noreferrer";$("job-status").parentElement.append(workLink);}
  const portIndex=job?.argv.indexOf("--port")??-1, port=portIndex>=0?Number(job.argv[portIndex+1]):0;
  workLink.hidden=!(job?.serving && job.status==="running" && Number.isInteger(port) && port>0 && port<=65535);
  if(!workLink.hidden) workLink.href=`http://127.0.0.1:${port}/?view=work`;
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
function message(text,kind="assistant",project=state.project?.id) {
  const node=el("div",text,"message "+kind);builderConversation(project).host.append(node);node.scrollIntoView({block:"nearest"});return node;
}

/** Generate a real model proposal, with source sharing and apply kept separate. */
async function sendPrompt(event) {
  event.preventDefault();
  if(!state.project||state.prompting) return;
  if(state.dirty) throw new Error("Save your source edits before sending them to the model.");
  const project=state.project.id,prompt=$("prompt").value.trim(),model=$("model").value.trim();
  if(!prompt) return;
  state.prompting=true;$("send-prompt").disabled=true;showSide("assistant");
  const conversation=builderConversation(project),history=conversation.history.slice(-6);
  conversation.history.push({role:"user",text:prompt.slice(0,8000)});
  preference("model",model);message(prompt,"user");
  const pending=message("Harnest builder agent is reading source and preparing a code proposal…");pending.classList.add("pending");
  const controller=new AbortController(),activity=activityCard(pending,()=>controller.abort());
  try {
    const proposal=await sendDraft($("prompt"),submitted=>runBuilder({project,prompt:submitted,model,history,...assistantLimits(),paths:[...state.context],allow_related_source:$("allow-related-source").checked,allow_fused_discovery:$("allow-fused-discovery").checked},{signal:controller.signal,activity:text=>activity.update(text),transport:studio.transport}),()=>state.project?.id===project);
    conversation.history.push({role:"assistant",text:proposal.summary.slice(0,8000)});
    conversation.history=conversation.history.slice(-6);
    activity.finish(proposal.kind==="message"?"Builder replied":"Proposal ready for review");
    if(proposal.kind==="message")message(proposal.summary,"assistant",project);else proposalCard(project,proposal);
  } catch(problem) {activity.finish(problem.name==="AbortError"?"Stopped — no changes applied":problem.message);if(problem.name!=="AbortError")pending.classList.add("error");}
  finally {state.prompting=false;$("send-prompt").disabled=!state.project;}
}

/** Retain proposals with their original project identity across navigation. */
function proposalCard(project,proposal) {
  const card=el("section","","proposal-card");
  card.append(el("h3",`✧ ${proposal.files.length} proposed file changes`),el("p",proposal.summary),el("p",`Project: ${project}`));
  if(proposal.context_paths) card.append(el("p",`Source read: ${proposal.context_paths.join(", ")}`));
  for(const file of proposal.files) card.append(el("div",(file.revision?"~ ":"+ ")+file.path,"proposal-path"));
  const review=button("Review changes ↗",()=>{if(proposal.mcp_review){requireSavedForMCP();return reviewMCP(api,proposal,async()=>{recordApplied(project,proposal,review);await refreshAppliedProject(project);});}return reviewProposal(project,proposal,review);},"button primary");card.append(review);builderConversation(project).host.append(card);card.scrollIntoView({block:"nearest"});
}

/** Keep source buffers from being silently invalidated by an MCP configuration review. */
function requireSavedForMCP(){if(state.dirty)throw new Error("Save your source edits before configuring MCP connections.");}

/** Review every proposed file against its exact original text before one conflict-checked apply. */
function reviewProposal(project,proposal,reviewButton) {
  const form=modal("Review proposed changes",proposal.summary,"Apply to project",async()=>{
    if(state.dirty) throw new Error("Save your editor changes before applying a proposal.");
    if(proposal.pack_review)await api("packs/apply","POST",{review:proposal.pack_review});
    else await api("files","PUT",{project,files:proposal.files.map(({path,text,revision})=>({path,text,revision}))});
    recordApplied(project,proposal,reviewButton);
    await refreshAppliedProject(project);
  },true);
  const tabs=el("div","","review-tabs"),preview=el("div","","review-diff");form.append(tabs,preview);
  const choose=file=>{renderDiff(preview,file);for(const tab of tabs.children)tab.classList.toggle("active",tab.textContent===file.path);};
  for(const file of proposal.files) tabs.append(button(file.path,()=>choose(file),""));
  choose(proposal.files[0]);
}

/** Record confirmed writes in both the visible conversation and the builder's next-turn context. */
function recordApplied(project,proposal,reviewButton) {
  if(reviewButton.disabled)return;
  reviewButton.textContent="Applied to project ✓";reviewButton.disabled=true;
  const count=proposal.files.length,paths=proposal.files.map(file=>file.path);
  const title=`✓ Successfully applied ${count} ${count===1?"file":"files"}`;
  reviewButton.closest(".proposal-card").querySelector("h3").textContent=title;
  const note=message("","applied-note",project);note.setAttribute("role","status");
  note.append(el("strong",title),el("p",proposal.summary));
  const files=el("ul");for(const path of paths)files.append(el("li",path));note.append(files);
  note.append(el("p",proposal.mcp_review?"Restart your agent to use the connection.":"Saved to your project. Run your agent to try the changes.","small muted"));
  const conversation=builderConversation(project);
  conversation.history.push({role:"assistant",text:`Applied successfully to ${project}: ${paths.join(", ")}. ${proposal.summary}`.slice(0,8000)});
  conversation.history=conversation.history.slice(-6);
  note.scrollIntoView({block:"nearest"});
}

/** A refresh failure must not turn a committed edit into an invitation to apply it again. */
async function refreshAppliedProject(project) {
  status("Changes applied. Run your agent to try them in a conversation.");
  if(state.project?.id!==project)return;
  try {await refreshProject();if(state.document)await openFile(state.document.path,true);}
  catch(problem) {status(`Changes were saved, but the view could not refresh: ${problem.message}`);}
}

/** Apply local model-budget preferences over the validated host defaults. */
function assistantLimits() {
  return {timeout:Number(preference("ai-timeout") || state.workspace.llm.timeout),max_tokens:Number(preference("ai-max-tokens") || state.workspace.llm.max_tokens)};
}

/** Configure bounded provider budgets for subsequent proposals. */
function providerHelp() {
  let timeout,tokens;
  const form=modal("Connect your model","Build with AI runs a compiled Harnest agent grounded in the bundled Harnest authoring skills. It uses your chosen model through LiteLLM. Your company pack can supply model, endpoint, budgets, and CA trust defaults. Local environment settings override these defaults. Set credentials in the terminal that starts Studio, then restart Studio.","Save",async()=>{preference("ai-timeout",timeout.value);preference("ai-max-tokens",tokens.value);});
  const limits=assistantLimits();
  timeout=field(form,"Model timeout (seconds)",limits.timeout,{type:"number",min:1,max:1800,step:1,required:true,hint:"Per provider call. Range: 1–1800 seconds."});
  tokens=field(form,"Maximum output tokens",limits.max_tokens,{type:"number",min:1,max:131072,step:1,required:true,hint:"Your provider may impose a lower limit."});
  form.append(el("pre","export HARNEST_BUILDER_MODEL=provider/model\n\n# Use your provider's standard key environment variable,\n# or a builder-specific key:\nexport HARNEST_BUILDER_API_KEY=...\n\n# Optional compatible endpoint:\nexport HARNEST_BUILDER_API_BASE=https://your-endpoint/v1\n\n# Optional company CA certificates:\nexport HARNEST_BUILDER_CA_BUNDLE=/path/to/company-ca.pem","command-preview"));
  form.append(el("p","Provider requests include your prompt and selected source files. Credentials are read only by the local server.","empty-note"));
}

/** Bind single-instance controls once; source and job refreshes replace only their own content. */
function bind() {
  bindNavigation();bindEditor();bindJobs();bindAssistant();
  on($("new-project"),"click",()=>studio.commands.execute("studio.project.create"));
  on($("appearance"),"click",()=>studio.commands.execute("studio.appearance"));
  on($("step-build"),"click",()=>{setView("canvas");showSide("assistant");$("prompt").focus();});
  on($("step-connect"),"click",()=>{requireSavedForMCP();return mcpPanel(api,state.project,refreshProject);});
  on($("step-run"),"click",()=>setView("try"));
  on($("studio-packs"),"click",showPacks);
  on($("open-project"),"click",openFolder);on($("extensions"),"click",extensionPanel);
  on($("deployment"),"click",()=>studio.commands.execute("studio.deployment.open"));
  on($("mcp-connections"),"click",()=>{requireSavedForMCP();return mcpPanel(api,state.project,refreshProject);});
  on($("compile"),"click",()=>command({action:"compile"}));on($("run-menu"),"click",checksMenu);
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
  on($("workspace-focus"),"click",toggleWorkspaceFocus);
  on($("journey"),"click",event=>{if(event.target.closest("button:not(:disabled)"))$("workspace-guide").open=false;});
  on($("workspace-guide"),"keydown",event=>{if(event.key==="Escape"){$("workspace-guide").open=false;$("workspace-guide").querySelector("summary").focus();}});
  for(const key of ["components","files"]) $(key+"-tab").addEventListener("click",()=>setLibrary(key));
  for(const key of ["canvas","code","try"]) $(key+"-tab").addEventListener("click",()=>setView(key));
  for(const key of ["assistant","inspector"]) $(key+"-tab").addEventListener("click",()=>showSide(key));
}

/** Reclaim vertical space without remounting the editor or hiding view and panel navigation. */
function toggleWorkspaceFocus() {
  const focused=document.querySelector('.workspace').classList.toggle('is-focused');
  $("workspace-focus").setAttribute("aria-pressed",String(focused));
  $("workspace-focus").textContent=focused?"Exit focus":"Focus";
  $("workspace-focus").title=focused?"Show workspace headers and tools":"Hide workspace headers and tools";
  $("workspace-guide").open=false;
}

/** Keyboard save and indentation operate on real textarea source, preserving browser undo. */
function bindEditor() {
  $("code-editor").addEventListener("input",editorChanged);
  on($("save-file"),"click",saveFile);on($("reload-file"),"click",()=>openFile(state.document.path));
  window.addEventListener("keydown",event=>{if((event.metaKey||event.ctrlKey)&&event.key==="s"){event.preventDefault();saveFile().catch(error);}},{signal:lifecycle.signal});
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
  document.addEventListener("click",event=>{const node=event.target.closest("[data-prompt]");if(node){if(!state.project){createProject({prompt:node.dataset.prompt});return;}$("prompt").value=node.dataset.prompt;$("prompt").focus();}},{signal:lifecycle.signal});
}

const releases=[
  studio.commands.register("studio.project.create",options=>createProject(options)),
  studio.commands.register("studio.project.refresh",refreshProject),
  studio.commands.register("studio.file.open",path=>openFile(path)),
  studio.commands.register("studio.editor.isDirty",()=>state.dirty),
  studio.commands.register("studio.jobs.run",command),
  studio.commands.register("studio.notice",status),
  studio.commands.register("studio.proposal.review",(project,proposal)=>{showSide("assistant");proposalCard(project,proposal);}),
  studio.navigation.guard(()=>!state.dirty),
  studio.slots.register("workspace.tabs",registerView),
];
bind();
on($("connection-form"),"submit",reconnect);
window.addEventListener("hashchange",()=>{studio.authorize();connect();},{signal:lifecycle.signal});
await connect();

/** Present contributed workspace views as native tabs without exposing default feature state. */
function registerView(host,item) {
  const tab=button(item.title,()=>setView(item.id),"");
  tab.setAttribute("role","tab");tab.setAttribute("aria-selected","false");
  host.id="view-"+item.id.replaceAll("/","-");host.setAttribute("role","tabpanel");
  tab.id=host.id+"-tab";tab.setAttribute("aria-controls",host.id);host.setAttribute("aria-labelledby",tab.id);host.hidden=true;
  document.querySelector('.view-tabs').append(tab);modViews.set(item.id,{host,tab});
  document.querySelector('[data-studio-slot="workspace.tabs"]').hidden=true;
  return ()=>{tab.remove();modViews.delete(item.id);};
}


/** Present installed company catalogs without exposing arbitrary executable UI plugins. */
async function showPacks() {
  const result=await api("packs"),form=modal("Studio Packs","Company templates, components, services, skills, and configuration profiles.","Close",()=>{},true,{cancel:false});
  if(!result.resources.length){form.append(el("p","No company packs are loaded. Start Studio with --pack /path/to/pack or install your company’s Studio distribution.","empty-note"));return;}
  const search=field(form,"Find a resource","",{placeholder:"Search templates, tools, skills…"}),list=el("div","","pack-resources");form.append(list);
  const render=()=>{
    list.replaceChildren();
    for(const item of result.resources.filter(item=>`${item.title} ${item.description} ${item.kind} ${item.pack}`.toLowerCase().includes(search.value.toLowerCase()))) {
      const card=el("section","","pack-resource");card.append(el("h3",item.title),el("p",`${item.pack} · ${item.version} · ${item.kind}`,"small muted"),el("p",item.description));
      if(item.kind==="builder-skill")card.append(el("span","Active builder guidance","tag"));
      else if(item.creates_agent)card.append(button("Create agent…",()=>createPackAgent(item)));
      else {const install=button("Review installation…",()=>previewPack(item));install.disabled=!state.project;card.append(install);}
      list.append(card);
    }
    if(!list.children.length)list.append(el("p","No matching resources.","empty-note"));
  };
  search.addEventListener("input",render);render();
}

/** Capture the new agent destination before requesting a source-only template preview. */
function createPackAgent(item) {
  let name,directory;
  const form=modal(item.title,"Create an editable agent from this company template. Review its files before creating the folder.","Review template",async()=>{
    const proposal=await api("packs/preview","POST",{resources:[item.id],name:name.value,directory:directory.value});
    reviewPack(proposal);return false;
  });
  name=field(form,"Agent name","",{required:true,pattern:"[a-z][a-z0-9-]{0,62}",placeholder:"my-agent"});
  directory=field(form,"Parent folder",state.workspace.path,{required:true});
}

/** Keep unsaved editor changes out of pack mutation, just like ordinary source proposals. */
async function previewPack(item) {
  if(state.dirty)throw new Error("Save your source changes before installing a pack resource.");
  const proposal=await api("packs/preview","POST",{project:state.project.id,resources:[item.id]});
  reviewPack(proposal);
}

/** Apply a server-owned pack snapshot only after reviewing its native source diff. */
function reviewPack(proposal) {
  if(!proposal.files.length) {
    modal("Already up to date",proposal.summary,"Done",()=>{},false,{cancel:false});
    $("dialog").classList.add("pack-current-dialog");
    return;
  }
  const form=modal("Review pack changes",proposal.summary,"Apply changes",async()=>{
    if(state.dirty)throw new Error("Save your source changes before applying a pack resource.");
    const result=await api("packs/apply","POST",{review:proposal.review});
    const summary=`Applied ${proposal.resources.join(", ")} successfully to ${result.project}.`;
    builderConversation(result.project).history.push({role:"assistant",text:summary.slice(0,8000)});
    message(summary,"applied-note",result.project);
    try {await refreshWorkspace();await openProject(result.project,true);status("Pack changes applied successfully.");}
    catch(problem){status(`Pack changes were saved, but the view could not refresh: ${problem.message}`);}
  },true);
  $("dialog").classList.add("pack-review-dialog");
  const sourceFiles=proposal.files.filter(file=>file.path!=="studio-packs.lock");
  const receipt=proposal.files.find(file=>file.path==="studio-packs.lock");
  if(sourceFiles.length) {
    const tabs=el("div","","review-tabs"),preview=el("div","","review-diff");form.append(tabs,preview);
    const choose=file=>{renderDiff(preview,file);for(const tab of tabs.children)tab.classList.toggle("active",tab.textContent===file.path);};
    for(const file of sourceFiles)tabs.append(button(file.path,()=>choose(file),""));
    choose(sourceFiles[0]);
  } else form.append(el("p","Your agent files are unchanged. Only the installed pack record will be updated.","pack-review-note"));
  if(receipt) {
    const details=el("details","","pack-receipt"),preview=el("div","","review-diff");
    details.append(el("summary","Installation record · studio-packs.lock"),preview);
    details.addEventListener("toggle",()=>{if(details.open&&!preview.children.length)renderDiff(preview,receipt);});
    form.append(details);
  }
}


/** Company resources appear beside native components and use the same catalog as the builder. */
function renderPackLibrary(host,search) {
  const groups=new Map();
  for(const item of state.workspace?.packs||[]) {
    if(item.kind==="builder-skill"||!`${item.title} ${item.description} ${item.pack}`.toLowerCase().includes(search))continue;
    if(!groups.has(item.pack)){const group=el("section","","library-group");group.append(el("h3",item.pack));host.append(group);groups.set(item.pack,group);}
    const card=button("",()=>item.creates_agent?createPackAgent(item):previewPack(item),"palette-card");
    card.title=item.description;card.disabled=!item.creates_agent&&!state.project;
    card.append(el("span","▧","palette-icon"),el("strong",item.title));groups.get(item.pack).append(card);
  }
}

return ()=>{lifecycle.abort();clearTimeout(pollTimer);clearTimeout(sourceEditor.timer);sourceEditor.generation++;sourceEditor.view.destroy();rightPanel.dispose();releases.forEach(release=>release());};
}
