import assert from 'node:assert/strict';
import test from 'node:test';
import {deploymentResult,deploymentState,accessURL} from '../src/harnest_builder/static/deployment.js';

test('a successful plan or recorded revision never claims the agent is running',()=>{
  assert.equal(deploymentState({status:'not-deployed'},null).ready,false);
  assert.equal(deploymentState({status:'ready',deployment:{}},null).ready,false);
  assert.equal(deploymentState({}, {components:[]}).ready,false);
  assert.equal(deploymentState({}, {status:'ready'}).ready,true);
  for(const status of ['failed','stopped','removed','not-deployed','degraded','missing','incomplete'])assert.equal(deploymentState({}, {status}).ready,false);
});

test('structured CLI results survive audit prefixes without accepting arbitrary output',()=>{
  const result={status:'ready',deployment:{access:[]}};
  assert.deepEqual(deploymentResult('{"event":"audit"}\n'+JSON.stringify(result,null,2)),result);
  assert.deepEqual(deploymentResult('{"components":[]}'),{components:[]});
  assert.throws(()=>deploymentResult('command failed'));
  assert.throws(()=>deploymentResult('{"event":"audit"}'));
});

test('only generated loopback HTTP endpoints become links',()=>{
  assert.equal(accessURL({url:'http://127.0.0.1:1907'}),'http://127.0.0.1:1907/');
  assert.equal(accessURL({url:'https://127.0.0.1:2907'}),'https://127.0.0.1:2907/');
  for(const url of ['javascript:alert(1)','https://attacker.test','http://user:secret@127.0.0.1',null,'tcp://127.0.0.1:9000'])assert.equal(accessURL({url}),null);
});
