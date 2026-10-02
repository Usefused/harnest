import assert from 'node:assert/strict';
import test from 'node:test';
import {cronFields,evaluationFields,runtimeFields,executionFields} from '../src/harnest_builder/ui_packs/default/assets/components.js';

class Element extends EventTarget {
  children=[];value='';hidden=false;
  constructor(tag){super();this.tag=tag;}
  setAttribute(name,value){this[name]=String(value);}
  append(...nodes){for(const node of nodes){node.parentElement=this;this.children.push(node);if(this.tag==='select'&&this.children.length===1)this.value=node.value;}}
}
globalThis.document={createElement:tag=>new Element(tag)};
const controls=form=>form.children.filter(node=>node.tag==='label').map(node=>node.children[1]);

test('cron modes expose and submit only their own policy with or without existing tasks',()=>{
  const form=new Element('form'),read=cronFields(form,['tasks/report.py','tasks/_private.py','tools/lookup.py']);
  const [mode,schedule,task,args,queue,retries]=controls(form);
  assert.equal(mode.tag,'select');
  assert.equal(task.children.length,1);
  assert.equal(task.parentElement.hidden,true);
  assert.deepEqual(read(),{mode:'fixed',schedule:'0 9 * * *',arguments:'{"payload": "scheduled"}',queue:'default',max_retries:'3'});
  mode.value='dynamic';mode.dispatchEvent(new Event('change'));
  assert.equal(schedule.required,false);assert.equal(args.required,false);
  assert.equal(schedule.disabled,true);assert.equal(args.disabled,true);
  assert.equal(schedule.parentElement.hidden,true);
  assert.deepEqual(read(),{mode:'dynamic',queue:'default',max_retries:'3'});
  retries.value='101';mode.value='existing';mode.dispatchEvent(new Event('change'));
  assert.equal(task.required,true);assert.equal(queue.required,false);assert.equal(retries.required,false);
  assert.equal(task.disabled,false);assert.equal(retries.disabled,true);
  assert.deepEqual(read(),{mode:'existing',schedule:'0 9 * * *',arguments:'{"payload": "scheduled"}',task:'report'});
  const empty=new Element('form');cronFields(empty,[]);
  assert.deepEqual(controls(empty)[0].children.map(node=>node.value),['fixed','dynamic']);
});

test('run actions exclude hidden invalid fields and preserve distinct evaluation and sync policies',()=>{
  const form=new Element('form'),action={value:'serve'},read=executionFields(form,action);
  const [input,port,extension,trajectory,profile]=controls(form);
  assert.deepEqual(read(),{port:1907});assert.equal(port.disabled,false);
  port.value='';action.value='test';form.dispatchEvent(new Event('change'));
  assert.equal(port.disabled,true);assert.deepEqual(read(),{});
  action.value='eval';form.dispatchEvent(new Event('change'));trajectory.value='strict';
  assert.equal(trajectory.disabled,false);assert.equal(profile.disabled,true);
  assert.deepEqual(read(),{eval_trajectory:'strict'});
  action.value='sync';form.dispatchEvent(new Event('change'));profile.value='eval';
  assert.equal(trajectory.disabled,true);assert.equal(profile.disabled,false);
  assert.deepEqual(read(),{environment_profile:'eval'});
  action.value='run';form.dispatchEvent(new Event('change'));input.value='Hello';
  assert.equal(input.required,true);assert.deepEqual(read(),{input:'Hello'});
  action.value='install-extension';form.dispatchEvent(new Event('change'));extension.value='postgres';
  assert.equal(extension.required,true);assert.deepEqual(read(),{name:'postgres'});
});

test('evaluation choices come from CLI metadata and storage starts with explicit local memory',()=>{
  const form=new Element('form');
  const metric=evaluationFields(form,[{id:'response_match_score',label:'Response similarity',backend:'local comparison'},{id:'custom',label:'Custom metric',backend:'Python'}]);
  assert.equal(metric.value,'response_match_score');
  assert.deepEqual(metric.children.map(node=>node.value),['response_match_score','custom']);
  const storage=new Element('form'),read=runtimeFields('task-storage',storage,[]);
  assert.deepEqual(read(),{provider:'memory'});
  controls(storage)[0].value='postgres';assert.deepEqual(read(),{provider:'postgres'});
});
