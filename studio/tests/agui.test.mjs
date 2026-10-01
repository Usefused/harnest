import assert from 'node:assert/strict';
import test from 'node:test';
import {readEvents,runBuilder} from '../src/harnest_builder/static/agui.js';

function response(text,split=1) {
  const bytes=new TextEncoder().encode(text);
  return new Response(new ReadableStream({start(controller){for(let i=0;i<bytes.length;i+=split)controller.enqueue(bytes.slice(i,i+split));controller.close();}}));
}
const frame=event=>`data: ${JSON.stringify(event)}\r\n\r\n`;

test('AG-UI parser handles UTF-8 and CRLF split across network reads',async()=>{
  const expected=[{type:'CUSTOM',name:'studio.activity',value:{message:'Checking café ☕'}},{type:'RUN_FINISHED'}],events=[];
  await readEvents(response(': keepalive\r\n\r\n'+expected.map(frame).join('')),event=>events.push(event));
  assert.deepEqual(events,expected);
});

test('only completed proposals are reviewable and run errors remain visible',async t=>{
  const proposal={summary:'Ready',files:[]};
  const emitted=[{type:'CUSTOM',name:'studio.proposal',value:proposal}];
  let sent;
  t.mock.method(globalThis,'fetch',async(url,options)=>{sent=JSON.parse(options.body);return response(emitted.map(frame).join(''));});
  await assert.rejects(runBuilder({project:'sample',prompt:'Improve it'}),/disconnected/);
  assert.equal(sent.forwardedProps.project,'sample');
  assert.equal(sent.messages[0].content,'Improve it');
  emitted.push({type:'RUN_FINISHED'});
  assert.deepEqual(await runBuilder({prompt:'Improve it'}),proposal);
  emitted.splice(0,emitted.length,{type:'RUN_ERROR',message:'Provider unavailable'});
  await assert.rejects(runBuilder({prompt:'Improve it'}),/Provider unavailable/);
});
