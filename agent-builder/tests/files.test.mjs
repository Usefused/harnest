import assert from 'node:assert/strict';
import test from 'node:test';
import {buildFileTree} from '../src/harnest_builder/static/ui.js';

test('source files retain their hierarchy, exact paths, and folders-first natural ordering',()=>{
  const tree=buildFileTree(['agent.py','tools/task10.py','tools/nested/check.py','tools/task2.py','config.yaml','lib/check.py']);
  assert.deepEqual(tree.map(node=>node.name),['lib','tools','agent.py','config.yaml']);
  const tools=tree[1];
  assert.deepEqual(tools.children.map(node=>node.name),['nested','task2.py','task10.py']);
  assert.deepEqual(tools.children[0].children,[{name:'check.py',path:'tools/nested/check.py',children:null}]);
  assert.equal(tree[0].children[0].path,'lib/check.py');
});

test('search reveals ancestors and matches folder paths without unrelated branches',()=>{
  const files=['agent.py','tools/nested/check.py','tools/search.py','lib/check.py'];
  const matches=buildFileTree(files,' CHECK.PY ');
  assert.deepEqual(matches.map(node=>node.name),['lib','tools']);
  assert.deepEqual(matches[1].children.map(node=>node.name),['nested']);
  assert.equal(buildFileTree(files,'tools/')[0].children.length,2);
  assert.deepEqual(buildFileTree(files,'missing'),[]);
  assert.equal(buildFileTree(files).length,3);
});

test('dotfiles and object-property names remain ordinary source names',()=>{
  const tree=buildFileTree(['__proto__/constructor.py','.config/settings.json','.env.example']);
  assert.equal(tree.find(node=>node.name==='__proto__').children[0].path,'__proto__/constructor.py');
  assert.equal(tree.find(node=>node.name==='.env.example').children,null);
  assert.deepEqual(buildFileTree([]),[]);
});
