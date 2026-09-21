import {el, button, field, select, modal, renderDiff, status} from './ui.js';

/** Review the same immutable plan for both UI selections and model suggestions. */
export function reviewMCP(api, proposal, applied) {
  const details=proposal.connection;
  const form=modal('Review MCP connection', 'Applying this review may create a Fused server and a seven-day execution token. Runtime credentials stay in private local storage and are supplied only to this project’s Studio commands. Restart a running agent to use them.', 'Apply connection', async()=>{
    await api('mcp/apply','POST',{review:proposal.mcp_review});
    await applied();status('MCP connection saved. Restart the agent to load its runtime bindings.');
  },true);
  form.append(el('p',`Owner: ${details.owner} · ${details.kind} · ${details.name}`));
  if(details.owner_team)form.append(el('p',`Fused owner team: ${details.owner_team}`));
  if(details.url)form.append(el('p',details.url));
  if(details.configuration)form.append(el('pre',JSON.stringify(details.configuration,null,2),'command-preview'));
  form.append(el('p',`Runtime variables: ${details.credential_variables.join(', ')}. Outside Studio, supply these through your own secret manager.`));
  for(const file of proposal.files){form.append(el('h3',file.path));const diff=el('div','','review-diff');renderDiff(diff,file);form.append(diff);}
}

/** Expose project configuration in the builder while keeping OAuth tokens on the host. */
export async function mcpPanel(api, project, applied) {
  const connection=await api('mcp/status');
  const form=modal('MCP connections', 'Connect an existing HTTP endpoint or create an Engine-hosted MCP with Fused. All source and provisioning changes are reviewed before applying.', 'Done', async()=>{},true);
  form.append(el('p',connection.connected?'Connected to Fused':'Fused workspace is not connected.'));
  const engine=field(form,'Fused Engine URL',connection.engine_url,{type:'url',placeholder:'https://your-fused-engine'});
  form.append(button(connection.connected?'Reconnect Fused':'Connect Fused',async()=>{
    const result=await api('mcp/connect','POST',{engine_url:engine.value.trim()});
    const link=el('a','Continue to Fused sign-in');link.href=result.url;link.target='_blank';link.rel='noopener noreferrer';link.className='button primary';
    form.append(link,el('p','After signing in, close this dialog and reopen MCP connections to refresh.'));
  }));
  if(connection.connected)form.append(button('Disconnect Fused',async()=>{await api('mcp/disconnect','POST');await mcpPanel(api,project,applied);}));
  form.append(button('Add HTTP MCP endpoint',()=>httpForm(api,project,applied)));
  if(connection.connected){
    form.append(button('Add existing Fused MCP',()=>existingForm(api,project,applied)));
    form.append(button('Create MCP with Fused',()=>createForm(api,project,applied)));
  }
}

/** Keep destination ownership and deployment target explicit for multi-agent projects. */
function destination(form,project){
  const resource=field(form,'Resource name','fused',{required:true,pattern:'[a-z][a-z0-9_]{0,62}'});
  const owner=select(form,'Agent owner',project.files.filter(path=>path==='agent.py'||path.endsWith('/agent.py')).map(path=>[path,path]),'agent.py');
  const deployment=field(form,'Deployment agent name','',{hint:'Optional for a single-agent deployment; required when the manifest contains several agents.'});
  return ()=>({project:project.id,resource:resource.value,owner:owner.value,deployment_agent:deployment.value});
}

/** Keep an explicitly entered bearer token out of source previews and model conversations. */
function httpForm(api,project,applied){
  let values,url,token;
  const form=modal('Add HTTP MCP endpoint','Use the actual Streamable HTTP endpoint. Credentials are submitted only to your local Studio server.','Review connection',async()=>{
    const proposal=await api('mcp/plan','POST',{...values(),kind:'http',url:url.value.trim(),bearer:token.value});
    token.value='';reviewMCP(api,proposal,applied);return false;
  });
  values=destination(form,project);url=field(form,'MCP URL','',{required:true,type:'url'});
  token=field(form,'Bearer token (optional)','',{type:'password',autocomplete:'off'});
}

/** Page real server metadata instead of constructing or guessing transport URLs. */
async function existingForm(api,project,applied,offset=0){
  const listing=await api(`mcp/discover?action=servers&offset=${offset}`);
  let values,server;
  const choices=listing.items.filter(item=>item.active);
  const form=modal('Add existing Fused MCP','Applying the review issues a new execution token for the selected server.','Review connection',async()=>{
    const chosen=choices[Number(server.value)];if(!chosen)throw new Error('Select an active server.');
    reviewMCP(api,await api('mcp/plan','POST',{...values(),kind:'existing',name:chosen.name,version:chosen.version,server_offset:offset}),applied);return false;
  });
  values=destination(form,project);server=select(form,'Server version',choices.map((item,index)=>[String(index),`${item.name} · ${item.version}`]));
  if(!choices.length)form.append(el('p','No active servers on this page.'));
  if(offset)form.append(button('Previous servers',()=>existingForm(api,project,applied,Math.max(0,offset-50))));
  if(offset+listing.items.length<listing.total)form.append(button('More servers',()=>existingForm(api,project,applied,offset+50)));
}

/** Select exact discovered services and operations; selecting all is always explicit. */
async function createForm(api,project,applied){
  const listing=await api('mcp/discover?action=services');
  const selections=new Map();let values,name,description,bucket,owner,version;
  const form=modal('Create MCP with Fused','Select services and their allowed operations. The next screen shows the full configuration before provisioning.','Review configuration',async()=>{
    const services=[...selections.values()].map(item=>({slug:item.slug,version:item.version,select_all:item.all.checked,operations:item.all.checked?[]:[...item.checks].filter(([,check])=>check.checked).map(([id])=>id)}));
    const proposal=await api('mcp/plan','POST',{...values(),kind:'create',name:name.value,description:description.value,bucket:bucket.value,owner_team:owner.value||null,version:version.value,services});
    reviewMCP(api,proposal,applied);return false;
  },true);
  values=destination(form,project);name=field(form,'Server name','',{required:true});description=field(form,'Description','');
  version=field(form,'Server version','1.0.0',{required:true});bucket=field(form,'Bucket','default',{required:true});owner=field(form,'Owner team (optional)','');
  const picker=select(form,'Workspace service',listing.items.map((item,index)=>[String(index),`${item.name} · ${item.version}`]));
  const selected=el('div','','dialog-form');
  form.append(button('Add selected service',async()=>{
    const item=listing.items[Number(picker.value)];if(!item)throw new Error('No service is available.');
    if(selections.has(item.slug))throw new Error('Remove the selected version before choosing another.');
    const operations=await api(`mcp/discover?action=operations&service_id=${encodeURIComponent(item.id)}&version=${encodeURIComponent(item.version)}`);
    const group=el('fieldset');group.append(el('legend',`${item.name} · ${item.version}`));
    const all=checkbox(group,'All operations (including future additions)',false),checks=new Map();
    for(const operation of operations.items)checks.set(operation.id,checkbox(group,operation.name||operation.id,false));
    all.addEventListener('change',()=>{for(const check of checks.values())check.disabled=all.checked;});
    group.append(button('Remove service',()=>{selections.delete(item.slug);group.remove();}));
    selections.set(item.slug,{...item,all,checks});selected.append(group);
  }),selected);
}

/** Associate operation controls with readable labels and native checkbox semantics. */
function checkbox(host,label,checked){
  const row=el('label','','context-option'),input=el('input');input.type='checkbox';input.checked=checked;row.append(input,document.createTextNode(label));host.append(row);return input;
}
