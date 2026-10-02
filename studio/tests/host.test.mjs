import assert from 'node:assert/strict';
import test from 'node:test';
import {apiURL,createState,createCommands,createSlots,frameState,appearanceSelection} from '../src/harnest_builder/static/host-api.js';

test('transport preserves query filenames while rejecting paths outside the local API',()=>{
  const origin='http://127.0.0.1:1940';
  assert.equal(apiURL('/api/file?path=notes..md',origin).search,'?path=notes..md');
  for(const path of ['https://other.invalid/api/file','/api/../assets/host.js','/api/%2e%2e/assets/host.js','/api/file#fragment'])assert.throws(()=>apiURL(path,origin),/Invalid/);
});

test('state snapshots cannot be mutated across contributions and unsubscribing stops delivery',()=>{
  const state=createState(),received=[];
  state.publish('project',{id:'one'});
  state.subscribe('project',value=>{value.id='changed';});
  const stop=state.subscribe('project',value=>received.push(value.id));
  state.publish('project',{id:'two'});stop();state.publish('project',{id:'three'});
  assert.deepEqual(received,['one','two']);
});

test('commands reject implicit replacement and disappear when their owner disposes',()=>{
  const commands=createCommands();
  const remove=commands.register('studio.create',value=>value+' result');
  assert.equal(commands.execute('studio.create','test'),'test result');
  assert.throws(()=>commands.register('studio.create',()=>{}),/already registered/);
  remove();assert.throws(()=>commands.execute('studio.create'),/Unknown/);
});

test('a failing subscriber cannot interrupt other UI contributions',()=>{
  const errors=[],values=[],state=createState(error=>errors.push(error.message));
  state.subscribe('project',()=>{throw new Error('broken mod');});
  state.subscribe('project',value=>values.push(value.id));
  state.publish('project',{id:'sample'});
  assert.deepEqual(values,['sample']);assert.deepEqual(errors,['broken mod']);
});

test('frame state never includes source, paths, credentials or server error detail',()=>{
  assert.deepEqual(frameState('project',{id:'one',name:'One',framework:'adk',path:'/private/project',source:'secret'}),{id:'one',name:'One',framework:'adk'});
  assert.deepEqual(frameState('connection',{ready:false,message:'private server detail'}),{ready:false});
  assert.throws(()=>frameState('credentials',{}),/Unsupported/);
});

test('shell slot adapters receive contribution metadata and can release ownership',()=>{
  const slots=createSlots(),host={},item={id:'team/notes'};
  const release=slots.register('workspace.tabs',(element,contribution)=>({element,contribution}));
  assert.deepEqual(slots.attach('workspace.tabs',host,item),{element:host,contribution:item});
  assert.throws(()=>slots.register('workspace.tabs',()=>{}),/already registered/);
  release();assert.equal(slots.attach('workspace.tabs',host,item),undefined);
});

test('appearance composes one theme over defaults and recovers from stale selections',()=>{
  const catalog={themes:[{id:'fused-studio/default',tokens:{bg:'#000',accent:'#fff'}},{id:'mod/blue',tokens:{accent:'#00f'}}],layouts:[{id:'fused-studio/default',order:['sidebar','workspace','inspector'],hidden:[]}]};
  const selected=appearanceSelection(catalog,{theme:'mod/blue',layout:'deleted/layout'});
  assert.deepEqual(selected.tokens,{bg:'#000',accent:'#00f'});
  assert.equal(selected.layout,'fused-studio/default');
  assert.equal(appearanceSelection(catalog,{theme:'deleted/theme'}).theme,'fused-studio/default');
});
