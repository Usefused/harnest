import assert from 'node:assert/strict';
import test from 'node:test';
import {sourceOffset,editorDiagnostics} from '../frontend/positions.mjs';

test('Ruff Unicode columns map to UTF-16 positions on the correct line',()=>{
  const source='title = "🦊 café"\nprint(missing)\n';
  assert.equal(sourceOffset(source,{row:1,column:12}),12);
  assert.equal(sourceOffset(source,{row:2,column:7}),source.indexOf('missing'));
  assert.equal(sourceOffset(source,{row:99,column:99}),source.length);
});

test('incomplete syntax at EOF keeps diagnostics in bounds and source messages inert',()=>{
  const source='def broken(';
  assert.deepEqual(editorDiagnostics(source,[{code:'invalid-syntax',message:'Expected <expression>',severity:'error',start:{row:1,column:99},end:{row:1,column:1}}]),[
    {from:source.length,to:source.length,severity:'error',message:'invalid-syntax: Expected <expression>',source:'Ruff'},
  ]);
});
