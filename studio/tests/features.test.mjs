import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {deploymentEnabled,renderDeploymentControl} from '../src/harnest_builder/static/features.js';

test('deployment stays invisible before metadata and without an explicit server boolean',()=>{
  for(const workspace of [null,{}, {features:{}}, {features:{deployment:false}}, {features:{deployment:'true'}}]) {
    const control={};
    renderDeploymentControl(control,workspace,{id:'agent'});
    assert.equal(control.hidden,true);
    assert.equal(control.disabled,true);
    assert.equal(deploymentEnabled(workspace),false);
  }
});

test('enabled deployment requires a selected project and can be hidden again',()=>{
  const control={};
  const workspace={features:{deployment:true}};
  renderDeploymentControl(control,workspace,null);
  assert.deepEqual(control,{hidden:false,disabled:true});
  renderDeploymentControl(control,workspace,{id:'agent'});
  assert.deepEqual(control,{hidden:false,disabled:false});
  renderDeploymentControl(control,{features:{deployment:false}},{id:'agent'});
  assert.deepEqual(control,{hidden:true,disabled:true});
});

test('initial HTML hides Deploy and generic source review does not advertise it',()=>{
  const html=readFileSync(new URL('../src/harnest_builder/static/index.html',import.meta.url),'utf8');
  const source=readFileSync(new URL('../src/harnest_builder/static/app.js',import.meta.url),'utf8');
  assert.match(html,/<button id="deployment"[^>]*\bhidden\b/);
  assert.ok(!source.includes('Changes applied. Preview the deployment plan'));
  assert.ok(!source.includes('import {showDeployment} from'));
});
