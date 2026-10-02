import assert from 'node:assert/strict';
import test from 'node:test';
import {createState,createCommands} from '../src/harnest_builder/static/host-api.js';
import {registerDeployment,deploymentJobs} from '../src/harnest_builder/ui_packs/default/assets/deployment-service.js';

test('deployment command respects server authority and selection, then releases ownership for replacement',async()=>{
  const studio={state:createState(),commands:createCommands(),request:async()=>({features:{deployment:false}})};
  const opened=[],dispose=registerDeployment(studio,project=>opened.push(project.id));
  await assert.rejects(()=>studio.commands.execute('studio.deployment.open'),/Select a project/);
  studio.state.publish('project',{id:'alpha'});
  await assert.rejects(()=>studio.commands.execute('studio.deployment.open'),/disabled/);
  studio.request=async()=>({features:{deployment:true}});
  await studio.commands.execute('studio.deployment.open');assert.deepEqual(opened,['alpha']);
  dispose();assert.throws(()=>studio.commands.execute('studio.deployment.open'),/Unknown/);
  studio.commands.register('studio.deployment.open',()=>opened.push('replacement'));
  studio.commands.execute('studio.deployment.open');assert.deepEqual(opened,['alpha','replacement']);
});

test('late deployment opening cannot target a project selected before navigation or disposal',async()=>{
  let resolve;
  const studio={state:createState(),commands:createCommands(),request:()=>new Promise(done=>{resolve=done;})};
  const opened=[],dispose=registerDeployment(studio,project=>opened.push(project.id));
  studio.state.publish('project',{id:'alpha'});
  const first=studio.commands.execute('studio.deployment.open');
  studio.state.publish('project',{id:'beta'});resolve({features:{deployment:true}});await first;
  const second=studio.commands.execute('studio.deployment.open');
  dispose();resolve({features:{deployment:true}});await second;assert.deepEqual(opened,[]);
});

test('deployment watchers handle early completion, failures, repeated snapshots, and disposal',async()=>{
  const studio={state:createState(),commands:createCommands()},seen=[];
  studio.commands.register('studio.notice',message=>seen.push(message));
  const jobs=deploymentJobs(studio),success=job=>seen.push('success:'+job.id),failed=job=>seen.push('failed:'+job.id);
  studio.state.publish('jobs',[{id:'early',status:'succeeded'}]);
  jobs.watch({id:'early'},success,failed);await Promise.resolve();
  studio.state.publish('jobs',[{id:'early',status:'succeeded'}]);
  jobs.watch({id:'later'},success,failed);
  studio.state.publish('jobs',[{id:'later',status:'failed'}]);await Promise.resolve();
  jobs.watch({id:'disposed'},success,failed);jobs.dispose();
  studio.state.publish('jobs',[{id:'disposed',status:'succeeded'}]);await Promise.resolve();
  assert.deepEqual(seen,['success:early','failed:later']);
});
