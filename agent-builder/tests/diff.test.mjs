import assert from 'node:assert/strict';
import test from 'node:test';
import {diffLines,contextRows} from '../src/harnest_builder/static/ui.js';

function reconstruct(rows,type) {return rows.filter(row=>row.type!==type).map(row=>row.text).join('');}
for(const [name,before,after] of [
  ['replacement','alpha\nbeta\ngamma\n','alpha\nchanged\ngamma\n'],
  ['new file','','first\nlast'],
  ['deleted contents','first\nlast',''],
  ['unchanged','first\nlast','first\nlast'],
  ['blank lines','\na\n\n','\n\na\n'],
  ['duplicate lines','a\nb\na\nb\n','b\na\nb\na\n'],
  ['final newline','same\n','same'],
  ['line endings','same\r\n','same\n'],
  ['inert markup','<script>old</script>\n','<script>new</script>\n'],
]) test(`${name}: the diff reconstructs both exact source revisions`,()=>{
  const {rows}=diffLines(before,after);
  assert.equal(reconstruct(rows,'added'),before);
  assert.equal(reconstruct(rows,'removed'),after);
  assert.deepEqual(rows.filter(r=>r.oldLine!==null).map(r=>r.oldLine),Array.from({length:rows.filter(r=>r.type!=='added').length},(_,i)=>i+1));
});

test('an edit marks only the replaced line with correct old and new numbers',()=>{
  const {rows}=diffLines('a\nb\nc\n','a\nB\nc\n');
  assert.deepEqual(rows.map(r=>[r.type,r.oldLine,r.newLine]),[['same',1,1],['removed',2,null],['added',null,2],['same',3,3]]);
});

test('nearby edits share context and long unchanged sections collapse',()=>{
  const before=Array.from({length:30},(_,i)=>`line ${i}\n`),after=[...before];
  after[10]='changed 10\n';after[13]='changed 13\n';
  const {rows}=diffLines(before.join(''),after.join(''));
  const focused=contextRows(rows);
  assert.deepEqual(focused.filter(r=>r.type==='gap').map(r=>r.count),[7,13]);
  assert.equal(focused.filter(r=>r.type==='added').length,2);
  assert.equal(focused.filter(r=>r.type==='removed').length,2);
});

test('large rewrites use a bounded replacement and preserve shared context',()=>{
  const before='header\n'+'old\n'.repeat(1100)+'footer\n';
  const after='header\n'+'new\n'.repeat(1100)+'footer\n';
  const {rows,coarse}=diffLines(before,after);
  assert.equal(coarse,true);
  assert.equal(rows[0].type,'same');assert.equal(rows.at(-1).type,'same');
  assert.equal(reconstruct(rows,'added'),before);assert.equal(reconstruct(rows,'removed'),after);
});
