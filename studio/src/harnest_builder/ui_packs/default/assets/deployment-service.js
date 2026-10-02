import {modal as openModal} from './ui.js';
import {showDeployment} from './deployment.js';
import {configureDeployment} from './deployment-config.js';

/** Give one resolved contribution command ownership while capturing the selected project per opening. */
export function registerDeployment(studio,open) {
  let project=null,active=true,opening=false;
  const unsubscribe=studio.state.subscribe('project',value=>{project=value;});
  const release=studio.commands.register('studio.deployment.open',async()=>{
    if(!active||opening)return;
    const selected=project;
    if(!selected)throw new Error('Select a project before opening deployment.');
    opening=true;
    try {
      const workspace=await studio.request('workspace');
      if(!active||project?.id!==selected.id)return;
      if(!workspace.features?.deployment)throw new Error('Deployment is disabled on this Studio server.');
      await open(selected);
    } finally {opening=false;}
  });
  return ()=>{active=false;unsubscribe();release();};
}

/** Observe completions even when a job finishes before its caller attaches a watcher. */
export function deploymentJobs(studio) {
  const pending=new Map();let jobs=[],active=true;
  /** Remove each callback before invoking it so repeated job snapshots cannot replay completion. */
  function deliver() {
    for(const job of jobs) {
      if(job.status==='running'||!pending.has(job.id))continue;
      const {success,failed}=pending.get(job.id);pending.delete(job.id);
      Promise.resolve().then(()=>active&&(job.status==='succeeded'?success(job):failed(job))).catch(problem=>{
        if(active)studio.commands.execute('studio.notice',problem.message);
      });
    }
  }
  const unsubscribe=studio.state.subscribe('jobs',value=>{jobs=value;deliver();});
  return {
    watch(job,success,failed){pending.set(job.id,{success,failed});deliver();},
    dispose(){active=false;unsubscribe();pending.clear();},
  };
}

/** Own the default deployment dialog through the same public context available to replacement packs. */
export function activate(studio) {
  const dialog=document.createElement('dialog');dialog.id='studio-deployment-dialog';document.body.append(dialog);
  const jobs=deploymentJobs(studio),lifecycle=new AbortController();
  let generation=0,projectId=null;
  const stopProject=studio.state.subscribe('project',project=>{
    if(projectId===project?.id)return;
    projectId=project?.id;generation++;if(dialog.open)dialog.close();
  });
  const execute=(name,...args)=>studio.commands.execute(name,...args);
  const options={
    async api(...args){const current=generation,value=await studio.request(...args);lifecycle.signal.throwIfAborted();if(current!==generation)throw new Error('The selected project changed. Reopen deployment.');return value;},
    run:body=>execute('studio.jobs.run',body),
    isDirty:()=>execute('studio.editor.isDirty'),
    notice:message=>execute('studio.notice',message),
    reviewProposal:(project,proposal)=>execute('studio.proposal.review',project,proposal),
    watch:jobs.watch,
    modal:(title,description,label,submit,wide)=>openModal(title,description,label,submit,wide,{dialog}),
    edit:async()=>{dialog.close();await execute('studio.file.open','harnest-deployment.yaml');},
  };
  const release=registerDeployment(studio,async project=>{
    const inspection=await options.api('deployment/inspect?project='+encodeURIComponent(project.id));
    if(!inspection.existing){configureDeployment({...options,project:project.id,inspection});return;}
    await showDeployment({...options,project:project.id});
  });
  return ()=>{lifecycle.abort();release();stopProject();jobs.dispose();if(dialog.open)dialog.close();dialog.remove();};
}
