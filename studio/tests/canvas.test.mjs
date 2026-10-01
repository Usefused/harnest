import assert from 'node:assert/strict';
import test from 'node:test';
import {architectureNodes, resourcesForNode, Canvas} from '../src/harnest_builder/static/canvas.js';

const project = {
  id:'sample', config:{name:'Sample'},
  files:['agent.py','config.yaml','instructions.md','agent-card.yaml','pyproject.toml',
    'tools/search.py','tools/summarize.py','mcp/docs.py','extensions/docker/extension.py',
    'extensions/docker/extension.yaml','extensions/docker/lib/runtime.py',
    'extensions/demo/extension.py','extensions/demo/pyproject.toml','extensions/demo/__init__.py',
    'harnest.lock','tests/unit/test_agent.py'],
  graph:{available:true,nodes:[{id:'search',reference:'search',expression:'"search"'}],edges:[{source:'START',target:'search'}]},
};

test('collapsed architecture shows one node per capability, never individual package files',()=>{
  const nodes=architectureNodes(project);
  assert.deepEqual(nodes.filter(n=>n.group).map(n=>n.title),['Tools','MCP connections','Extensions','Tests']);
  assert.equal(nodes.length,9);
  assert.equal(nodes.find(n=>n.id==='category:extension').files.length,5);
  assert.equal(nodes.some(n=>n.id==='extensions/docker/extension.py'),false);
  for(const node of nodes.filter(n=>n.group)) assert.equal(node.parent,'agent.py');
});

test('expanding extensions reveals packages, then only the selected package and its nested folders',()=>{
  const expanded=new Set(['category:extension','folder:extensions/docker']);
  const nodes=architectureNodes(project,expanded);
  const ids=nodes.map(n=>n.id);
  assert.ok(ids.includes('folder:extensions/demo'));
  assert.ok(ids.includes('extensions/docker/extension.yaml'));
  assert.ok(ids.includes('folder:extensions/docker/lib'));
  assert.ok(!ids.includes('extensions/docker/lib/runtime.py'));
  assert.ok(!ids.includes('extensions/demo/extension.py'));
  assert.equal(nodes.find(n=>n.id==='extensions/docker/extension.yaml').parent,'folder:extensions/docker');
  expanded.add('folder:extensions/docker/lib');
  assert.ok(architectureNodes(project,expanded).some(n=>n.id==='extensions/docker/lib/runtime.py'));
  expanded.delete('category:extension');
  assert.equal(architectureNodes(project,expanded).filter(n=>n.kind==='extension').length,1);
});

test('multiple expanded categories allocate nonoverlapping rows and preserve explicit positions',()=>{
  const expanded=new Set(['category:tool','category:extension','folder:extensions/docker']);
  const nodes=architectureNodes(project,expanded);
  const slots=nodes.map(n=>`${n.x}:${n.y}`);
  assert.equal(new Set(slots).size,slots.length);
  const positioned=architectureNodes(project,expanded,{'category:tool':{x:900,y:900}});
  assert.equal(positioned.find(n=>n.id==='category:tool').x,900);
  assert.equal(positioned.find(n=>n.id==='category:tool').y,900);
});

test('source ownership edges link each visible file to its category or folder',()=>{
  const nodes=architectureNodes(project,new Set(['category:tool']));
  const edge=node=>Canvas.prototype.ownershipEdge(node);
  assert.deepEqual(edge(nodes.find(n=>n.id==='tools/search.py')),{source:'category:tool',target:'tools/search.py'});
  assert.deepEqual(edge(nodes.find(n=>n.id==='config.yaml')),{source:'config.yaml',target:'agent.py'});
});

test('explicit workflow topology still exposes executable nodes and original source paths',()=>{
  const canvas={positions:{},nodeSource:Canvas.prototype.nodeSource};
  const nodes=Canvas.prototype.workflowNodes.call(canvas,project);
  assert.deepEqual(nodes.map(n=>n.id),['START','search']);
  assert.equal(nodes[1].path,'tools/search.py');
  assert.equal(nodes.some(n=>n.group),false);
});

test('tool ownership follows nested agent scopes after a move instead of remaining on the root',()=>{
  const scoped={...project,files:['agent.py','tools/root_tool.py','subagents/helper/agent.py','subagents/helper/tools/lookup.py'],ownership:{available:true,agents:[{id:'agent.py',path:'agent.py',folder:'',flat:false,name:'Root agent'},{id:'subagents/helper/agent.py',path:'subagents/helper/agent.py',folder:'subagents/helper/',flat:false,name:'helper'}],resources:[{path:'tools/root_tool.py',kind:'tool',owner:'agent.py',name:'root_tool'},{path:'subagents/helper/tools/lookup.py',kind:'tool',owner:'subagents/helper/agent.py',name:'lookup'}]}};
  const expanded=new Set(['category:tool','category:subagent','subagents/helper/agent.py','category:tool:subagents/helper/agent.py']);
  const nodes=architectureNodes(scoped,expanded);
  const tool=nodes.find(n=>n.id==='subagents/helper/tools/lookup.py');
  assert.equal(tool.kind,'tool');
  assert.equal(tool.parent,'category:tool:subagents/helper/agent.py');
  assert.equal(tool.resource.owner,'subagents/helper/agent.py');
  assert.equal(nodes.find(n=>n.id===tool.parent).parent,'subagents/helper/agent.py');
  assert.equal(nodes.find(n=>n.id==='subagents/helper/agent.py').ownerId,'subagents/helper/agent.py');
  assert.deepEqual(nodes.find(n=>n.id==='category:tool').files,['tools/root_tool.py']);
  assert.equal(nodes.filter(n=>n.id===tool.id).length,1);
});

test('skill file and category edges resolve complete packages; subagent batches omit descendants',()=>{
  const resources=[{path:'skills/research',kind:'skill'},{path:'skills/writing',kind:'skill'},
    {path:'subagents/helper',anchor:'subagents/helper/agent.py',kind:'subagent'},
    {path:'subagents/helper/skills/local',kind:'skill'}];
  assert.deepEqual(resourcesForNode({path:'skills/research/references/guide.md'},resources),[resources[0]]);
  assert.deepEqual(resourcesForNode({path:'skills/',group:true},resources),resources.slice(0,2));
  assert.deepEqual(resourcesForNode({path:'subagents/',group:true},resources),[resources[2]]);
  assert.deepEqual(resourcesForNode({path:'subagents/helper/agent.py',group:true},resources),[resources[2]]);
  assert.deepEqual(resourcesForNode({path:'extensions/',group:true},resources),[]);
});

test('every edge reconnects either end directly and remains keyboard accessible',()=>{
  const original=globalThis.document;
  const element=(_namespace,tag)=>({tag,attributes:{},children:[],events:{},setAttribute(key,value){this.attributes[key]=value;},getAttribute(key){return this.attributes[key];},append(...items){this.children.push(...items);},addEventListener(name,callback){this.events[name]=callback;}});
  globalThis.document={createElementNS:element};
  try {
    const calls=[],canvas={mode:'architecture',point:event=>({x:event.x,y:event.y}),edgeDrag:(_event,edge,end)=>calls.push(end),handlers:{reconnect:(edge)=>calls.push(edge)}};
    const edge={source:'agent.py',target:'category:extension'};
    const path=element();path.setAttribute('d','M0,0 C1,0 2,0 3,0');
    const group=Canvas.prototype.edgeControl.call(canvas,path,edge,{x:0,y:0,title:'Root'},{x:300,y:0,title:'Extensions'});
    assert.equal(group.children.some(child=>child.tag==='circle'),false);
    group.events.pointerdown({x:222,y:39});
    group.events.pointerdown({x:298,y:39});
    assert.deepEqual(calls,['source','target']);
    group.events.click({stopPropagation(){}});
    assert.equal(calls[2],edge);
    group.events.keydown({key:'Enter',preventDefault(){},stopPropagation(){}});
    assert.equal(calls[3],edge);
    canvas.edgeDragged=true;
    group.events.click({stopPropagation(){}});
    assert.equal(calls.length,4);
  } finally {globalThis.document=original;}
});


test('pending pointer and keyboard connections reveal destinations until completed',()=>{
  const states=[],assigned=[];
  const canvas={mode:'architecture',root:{classList:{toggle:(name,value)=>states.push([name,value])}},handlers:{assign:(...args)=>assigned.push(args)},setPending:Canvas.prototype.setPending};
  canvas.setPending('tools/search.py');
  Canvas.prototype.finishConnection.call(canvas,'subagents/helper.py');
  assert.deepEqual(states,[['connecting',true],['connecting',false]]);
  assert.deepEqual(assigned,[['tools/search.py','subagents/helper.py']]);
  assert.equal(canvas.pending,null);
});


test('short forward edges keep their curve control points ordered',()=>{
  const start={x:220,y:39},end={x:280,y:39};
  const points=Canvas.prototype.curve(start,end).match(/[\d.]+/g).map(Number);
  assert.ok(points[2]>start.x && points[4]<end.x);
  assert.ok(points[2]<points[4]);
});
