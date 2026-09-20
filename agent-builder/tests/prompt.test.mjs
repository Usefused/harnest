import assert from 'node:assert/strict';
import test from 'node:test';
import {sendDraft} from '../src/harnest_builder/static/ui.js';

class Composer extends EventTarget {
  value='  Original prompt\n';
  type(value) {this.value=value;this.dispatchEvent(new Event('input'));}
}
function deferred() {
  let resolve,reject;
  const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
  return {promise,resolve,reject};
}

test('sending clears immediately, submits trimmed text and preserves the next draft',async()=>{
  const input=new Composer(),request=deferred();
  const result=sendDraft(input,prompt=>{assert.equal(prompt,'Original prompt');return request.promise;});
  assert.equal(input.value,'');
  input.type('Next prompt');
  request.resolve('proposal');
  assert.equal(await result,'proposal');
  assert.equal(input.value,'Next prompt');
});

test('a successful submission leaves an untouched composer empty',async()=>{
  const input=new Composer();
  await sendDraft(input,async()=> 'proposal');
  assert.equal(input.value,'');
});

test('failure restores the exact draft when the composer is untouched',async()=>{
  const input=new Composer();
  await assert.rejects(sendDraft(input,async()=>{throw new Error('offline');}),/offline/);
  assert.equal(input.value,'  Original prompt\n');
});

for(const next of ['New draft','']) test(`failure preserves subsequent user edits (${JSON.stringify(next)})`,async()=>{
  const input=new Composer(),request=deferred();
  const result=sendDraft(input,()=>request.promise);
  input.type('New draft');input.type(next);
  request.reject(new Error('offline'));
  await assert.rejects(result,/offline/);
  assert.equal(input.value,next);
});

test('failure does not restore a draft into another project',async()=>{
  const input=new Composer();
  await assert.rejects(sendDraft(input,async()=>{throw new Error('offline');},()=>false),/offline/);
  assert.equal(input.value,'');
});
