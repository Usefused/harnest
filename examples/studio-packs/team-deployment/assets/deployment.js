/** Replace the deployment flow with team-owned options, review, and an explicit apply action. */
export function activate(studio) {
  let project=null,selected=null,review=null,jobId=null,busy=false,active=true,generation=0;
  const dialog=document.createElement('dialog');dialog.className='wide';
  const heading=document.createElement('h2');heading.id='team-deployment-title';heading.textContent='Team deployment';
  dialog.setAttribute('aria-labelledby',heading.id);
  const label=document.createElement('label');label.textContent='Deployment environment';
  const environment=document.createElement('select');environment.setAttribute('aria-label','Deployment environment');
  for(const name of ['local','staging']){const option=document.createElement('option');option.value=name;option.textContent=name;environment.append(option);}
  label.append(environment);
  const output=document.createElement('pre');output.setAttribute('role','status');output.style.whiteSpace='pre-wrap';
  const actions=document.createElement('div');actions.className='dialog-actions';
  const refresh=document.createElement('button');refresh.textContent='Review plan';refresh.className='button secondary';
  const deploy=document.createElement('button');deploy.textContent='Deploy reviewed plan';deploy.className='button primary';deploy.disabled=true;
  const close=document.createElement('button');close.textContent='Close';close.className='button secondary';
  actions.append(close,refresh,deploy);dialog.append(heading,label,output,actions);document.body.append(dialog);
  const events=new AbortController(),options={signal:events.signal};

  /** Require a new review after any project or environment change; never reuse stale manifest revisions. */
  function invalidate(){generation++;review=null;deploy.disabled=true;output.textContent='Choose an environment, then review its plan.';}
  /** Disable mutable options while a job is running and keep blocked plans from being applied. */
  function controls(){environment.disabled=busy;refresh.disabled=busy;deploy.disabled=busy||!review||review.blockers.length>0;}

  /** Read configuration only; reviewing does not start a deployment. */
  async function readPlan() {
    invalidate();const current=generation;
    try {
      const value=await studio.request(`deployment/overview?project=${encodeURIComponent(selected.id)}&environment=${encodeURIComponent(environment.value)}`);
      if(!active||current!==generation)return;
      review=value;
      const components=value.plan.components.map(item=>`${item.name}: ${item.image||'Connected service'} · ${item.replicas||1} replica(s)`);
      output.textContent=[`Project: ${selected.name}`,`Environment: ${environment.value}`,`Target: ${value.plan.backend}`,'',...components,'',...value.blockers,value.blockers.length?'Resolve the requirements above before deploying.':'Ready for your review. No workloads have been started.'].join('\n');controls();
    } catch(error){if(active&&current===generation)output.textContent=error.message;}
  }

  /** Apply only the reviewed revision; the server still enforces feature gates and revision checks. */
  async function apply() {
    if(busy||!review)return;
    if(studio.commands.execute('studio.editor.isDirty')){output.textContent='Save your source edits before deploying.';return;}
    busy=true;controls();
    try {
      const job=await studio.commands.execute('studio.jobs.run',{action:'provision',operation:'apply',project:selected.id,environment:environment.value,manifest_revision:review.manifest_revision});
      if(!active)return;jobId=job.id;output.textContent='Deployment started. Follow its output in the terminal.';
    } catch(error){busy=false;output.textContent=error.message;controls();}
  }
  environment.addEventListener('change',invalidate,options);
  refresh.addEventListener('click',readPlan,options);deploy.addEventListener('click',apply,options);
  close.addEventListener('click',()=>dialog.close(),options);
  const stopProject=studio.state.subscribe('project',value=>{
    if(project?.id!==value?.id){invalidate();if(dialog.open)dialog.close();}
    project=value;
  });
  const stopJobs=studio.state.subscribe('jobs',jobs=>{
    const job=jobs.find(item=>item.id===jobId&&item.status!=='running');if(!job)return;
    jobId=null;busy=false;invalidate();output.textContent=`Deployment job ${job.status}.\n${job.output||''}`;controls();
  });
  const release=studio.commands.register('studio.deployment.open',async()=>{
    const requested=project;if(!requested)throw new Error('Select a project first.');
    const workspace=await studio.request('workspace');
    if(!active||project?.id!==requested.id)return;
    if(!workspace.features?.deployment)throw new Error('Deployment is disabled on this Studio server.');
    selected=requested;invalidate();controls();if(!dialog.open)dialog.showModal();
  });
  return ()=>{active=false;events.abort();release();stopProject();stopJobs();if(dialog.open)dialog.close();dialog.remove();};
}
