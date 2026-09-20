import {el,button,field,modal,preference} from './ui.js';

/** Decode the final CLI result while tolerating audit lines before the JSON document. */
export function deploymentResult(output) {
  const lines=output.trim().split('\n');
  for(let index=lines.length-1;index>=0;index--) {
    if(!lines[index].startsWith('{'))continue;
    try {const value=JSON.parse(lines.slice(index).join('\n'));if(value && (value.status || value.components || value.revisions))return value;} catch {}
  }
  throw new Error('The command finished without a readable deployment result. Check command output and refresh status.');
}

/** Distinguish reviewed intent from backend-confirmed readiness and historical state. */
export function deploymentState(recorded,live) {
  if(live?.status==='ready')return {title:'Deployment is running',ready:true};
  const titles={'not-deployed':'Not deployed',removed:'Not deployed',stopped:'Deployment stopped',degraded:'Deployment needs attention',missing:'Deployment resources are missing',incomplete:'Deployment is incomplete',failed:'Deployment failed',interrupted:'Deployment was interrupted'};
  if(live)return {title:titles[live.status]||'Deployment needs attention',ready:false};
  return {title:!recorded?.deployment||recorded.status==='removed'?'Ready to review · not deployed':'Deployment status not checked',ready:false};
}

/** Only generated HTTP loopback endpoints can become clickable browser links. */
export function accessURL(port) {
  try {const url=new URL(port.url);return ['http:','https:'].includes(url.protocol)&&url.hostname==='127.0.0.1'&&!url.username&&!url.password?url.href:null;} catch {return null;}
}

/** Present deployment review, progress, access, and recovery within one guided screen. */
export async function showDeployment(options) {
  const view=new DeploymentView(options);
  await view.load();
  if(view.form.isConnected && view.data?.recorded.deployment && view.data.recorded.status!=='removed')await view.launch('status');
}

class DeploymentView {
  /** Keep callbacks bound to the project and dialog that initiated the operation. */
  constructor(options) {
    Object.assign(this,options);this.live=null;this.busy=false;this.target=null;this.confirmation=null;
    this.form=modal('Deploy your agent','Review what will run, deploy it, then use the access details below.','Close',async()=>{},true);
    this.form.parentElement.querySelector('.dialog-actions').firstElementChild.remove();
    this.form.classList.add('deployment-panel');
    this.environment=field(this.form,'Environment',preference('deployment-environment:'+this.project)||'local',{placeholder:'local or a manifest environment'});
    this.environment.addEventListener('change',async()=>{this.live=null;this.target=null;this.preview=null;await this.load();if(this.data?.recorded.deployment&&this.data.recorded.status!=='removed')await this.launch('status');});
    this.body=el('div','','deployment-body');this.form.append(this.body);
  }

  /** Load an offline review; malformed configuration retains a visible route to edit it. */
  async load() {
    this.environment.disabled=true;
    this.body.replaceChildren(el('p','Reading deployment configuration…','empty-note'));
    try {
      this.data=await this.api('deployment/overview?project='+encodeURIComponent(this.project)+'&environment='+encodeURIComponent(this.environment.value));
      preference('deployment-environment:'+this.project,this.environment.value);this.problem='';this.render();
    } catch(problem) {this.data=null;this.fail(problem);}
    finally {this.environment.disabled=this.busy;}
  }

  /** Show actionable failures in the deployment screen instead of only in the terminal. */
  fail(problem) {this.problem=problem.message||String(problem);this.busy=false;this.environment.disabled=false;this.render();}

  /** Repaint only after an explicit review, operation, or status result. */
  render() {
    if(!this.form.isConnected)return;
    this.body.replaceChildren();
    if(this.problem)this.body.append(el('p',this.problem,'deployment-error'));
    if(!this.data) {this.body.append(button('Edit deployment configuration',this.edit));return;}
    const state=deploymentState(this.data.recorded,this.live);
    this.body.append(el('div','1 Configure  →  2 Review & deploy  →  3 Access your agent','deployment-steps'));
    const title=this.target?`Review rollback to revision ${this.target}`:!this.live&&this.data.blockers.length?'Configuration needs attention':state.title;
    this.body.append(el('h3',this.busy?this.progress:title,'deployment-title'));
    if(state.ready&&!this.busy) {
      this.body.append(el('p',`Active revision ${this.live.active_revision??'unknown'} · ${this.live.deployment.environment}`,'deployment-release'));
      this.access(true);
      this.body.append(button('Refresh status',()=>this.launch('status')));
      const next=el('details','','deployment-review');next.append(el('summary','Review and deploy an update'));
      this.review(next);this.actions(next,false);this.body.append(next);
    } else {this.review();this.actions();this.access(false);}
    this.history();this.management();
  }

  /** Summarize images, dependencies, and prerequisites without exposing credential values. */
  review(host=this.body) {
    const plan=this.preview||this.data.plan;
    host.append(el('p',`${plan.name} · ${plan.backend==='local'?'Local Docker':`Kubernetes · ${plan.context} / ${plan.namespace}`} · ${plan.environment}`));
    const list=el('div','','deployment-components');
    for(const component of plan.components) {
      const card=el('div','','deployment-component');card.append(el('strong',component.name));
      card.append(el('span',component.mode==='connect'?'Existing external service':`${component.kind==='agent'?'Agent':'Service'} · ${component.image} · ${component.replicas} replica(s)`));list.append(card);
    }
    host.append(list);
    host.append(el('p','Deployment runs container images. Build agent compiles source; it does not build a container image. Make sure each image is available to Docker or your cluster.','empty-note'));
    if(this.target)host.append(el('p',`Reviewing rollback to revision ${this.target}. This creates a new revision. Database contents and migrations are not rolled back.`,'deployment-notice'));
    if(!this.target)for(const message of this.data.blockers)host.append(el('p',message,'deployment-notice'));
  }

  /** Give the next action a named button instead of requiring knowledge of CLI operations. */
  actions(host=this.body,showRefresh=true) {
    const row=el('div','','deployment-actions');
    const label=this.target?`Roll back to revision ${this.target}`:this.data.recorded.active_revision?'Deploy update':'Deploy agent';
    const primary=button(this.busy?this.progress:label,()=>this.launch(this.target?'rollback':'apply'),'button primary');
    primary.disabled=this.busy||(!this.target&&this.data.blockers.length>0);row.append(primary);
    if(showRefresh) {const refresh=button('Refresh status',()=>this.launch('status'));refresh.disabled=this.busy;row.append(refresh);}
    const edit=button('Edit configuration',this.edit);edit.disabled=this.busy;row.append(edit);
    if(this.target) {const cancel=button('Cancel rollback',()=>{this.target=null;this.preview=null;this.render();});cancel.disabled=this.busy;row.append(cancel);}
    host.append(row);
    if(!this.busy&&!this.live)host.append(el('p','This is a configuration review. No containers have been started by opening this screen.','empty-note'));
  }

  /** Show recorded endpoint details only after confirmed readiness; planned addresses stay labelled. */
  access(ready) {
    const plan=ready?this.live.deployment:(this.preview||this.data.plan);
    const host=el('section','','deployment-access');host.append(el('h3',ready?'Access your agent':'Access after deployment'));
    if(!ready)host.append(el('p','These are planned connection details. Deploy successfully or refresh status before connecting.','empty-note'));
    for(const agent of plan?.access||[]) {
      const card=el('div','','deployment-endpoint');card.append(el('h4',agent.name));
      if(agent.note)card.append(el('p',agent.note));
      for(const port of agent.ports)this.endpoint(card,port,ready);
      host.append(card);
    }
    if(!plan?.access?.length)host.append(el('p','No recorded access details. Review the configured agent ports and deploy an update.','empty-note'));
    this.body.append(host);
  }

  /** Port-forward endpoints are instructions, never falsely advertised as public ingress. */
  endpoint(card,port,ready) {
    card.append(el('p',`${port.name} · ${port.scope==='port-forward'?'Kubernetes port-forward':port.scope==='local'?'Local endpoint':'Internal only'}`));
    if(port.command)this.copyable(card,port.command,'Copy port-forward command');
    const address=port.url||(port.host?`${port.host}:${port.port}`:'');
    if(address)this.copyable(card,address,'Copy endpoint');
    const url=accessURL(port);
    if(ready&&url) {const link=el('a',port.command?'Open after starting port-forward ↗':'Open agent endpoint ↗','button primary');link.href=url;link.target='_blank';link.rel='noopener noreferrer';card.append(link);}
    card.append(el('p',port.note,'empty-note'));
  }

  /** Copy only a visible address or bounded generated command, with a selectable fallback. */
  copyable(host,value,label) {
    const row=el('div','','deployment-copy');row.append(el('code',value));
    row.append(button(label,async()=>{await navigator.clipboard.writeText(value);this.notice('Copied to clipboard.');}));host.append(row);
  }

  /** Display release outcomes and successful rollback choices without manual revision-number entry. */
  history() {
    const host=el('details','','deployment-history');host.append(el('summary','Deployment history'));
    const history=this.data.history;
    if(!history.revisions.length)host.append(el('p','No deployments yet. Your first successful deployment will appear here.','empty-note'));
    for(const item of history.revisions) {
      const row=el('div','','deployment-revision');
      row.append(el('span',`Revision ${item.revision}${item.release?' · '+item.release:''} · ${item.status}${item.revision===history.active_revision?' · active':''}`));
      if(item.status==='succeeded') {const restore=button('Review rollback',()=>this.launch('plan',item.revision));restore.disabled=this.busy;row.append(restore);}
      host.append(row);
    }
    if(history.next_before)host.append(button('Load older revisions',async()=>{
      const page=await this.api(`deployment/history?project=${encodeURIComponent(this.project)}&environment=${encodeURIComponent(this.environment.value)}&before=${history.next_before}`);
      history.revisions.push(...page.revisions);history.next_before=page.next_before;this.render();this.body.querySelector('.deployment-history').open=true;
    }));
    this.body.append(host);
  }

  /** Keep disruptive maintenance actions separate from the normal deployment path. */
  management() {
    if(!this.data.recorded.deployment||this.data.recorded.status==='removed')return;
    const host=el('details','','deployment-management');host.append(el('summary','Manage deployment'));
    host.append(el('p','Stop pauses workloads. Remove deletes provisioned workloads and retains persistent volumes and connected external services.','empty-note'));
    for(const [operation,label] of [['stop','Stop deployment'],['remove','Remove deployment']]) {
      const action=button(label,()=>{this.confirmation=operation;this.render();});action.disabled=this.busy;host.append(action);
    }
    if(this.confirmation) {
      host.open=true;host.append(el('p',`Confirm ${this.confirmation}: running agents will become unavailable. Persistent data is retained.`,'deployment-notice'));
      host.append(button(`Confirm ${this.confirmation}`,()=>this.launch(this.confirmation),'button secondary danger'),button('Cancel',()=>{this.confirmation=null;this.render();}));
    }
    this.body.append(host);
  }

  /** Start a fixed CLI job, keeping this screen open through readiness checks and failures. */
  async launch(operation,revision=null) {
    if(this.busy)return;
    if(this.isDirty()&&!['status','plan'].includes(operation))throw new Error('Save your source changes before deploying.');
    this.busy=true;this.problem='';this.live=null;this.environment.disabled=true;
    this.progress={apply:'Deploying · waiting for readiness…',rollback:'Rolling back · waiting for readiness…',status:'Checking running workloads…',plan:'Preparing rollback review…',stop:'Stopping deployment…',remove:'Removing deployment…'}[operation];this.render();
    const selected=revision||this.target;
    try {
      const job=await this.run({action:'provision',project:this.project,operation,environment:this.environment.value,
        ...(selected&&['plan','rollback'].includes(operation)?{revision:selected}:{}),
        ...(operation==='apply'?{manifest_revision:this.data.manifest_revision}:{})});
      this.watch(job,async finished=>{
        try {await this.completed(operation,selected,deploymentResult(finished.output));}
        catch(problem){this.fail(problem);}
      },finished=>{this.live=null;this.target=null;this.preview=null;this.fail(new Error(`Deployment ${operation} ${finished.status}. ${finished.output?.trim()||'See command output for details.'}`));});
    } catch(problem) {this.fail(problem);}
  }

  /** Preserve access from the exact deployed revision even when authored configuration has changed. */
  async completed(operation,revision,result) {
    this.busy=false;this.environment.disabled=false;this.confirmation=null;
    if(operation==='plan') {this.target=revision;this.preview=result;this.live=null;this.render();return;}
    this.target=null;this.preview=null;this.live=result;
    if(this.form.isConnected)await this.load();
    this.notice(result.status==='ready'?'Deployment ready. Access details are shown in Deploy.':`Deployment status: ${result.status}.`);
  }
}
