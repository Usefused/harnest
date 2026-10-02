import assert from 'node:assert/strict';
import test from 'node:test';
import {commandText,initCommand,jobCommand} from '../src/harnest_builder/ui_packs/default/assets/commands.js';

test('company init preview retains argv, profile and configured pack options',()=>{
  const cli={command:['acme'],init_args:['--team','customer success']};
  assert.equal(initCommand(cli,{directory:'/tmp/Team work/',name:'agent',framework:'adk',mode:'advanced',profile:'example'}),"acme init '/tmp/Team work/agent' --framework adk --mode advanced --example --team 'customer success'");
  assert.equal(jobCommand({command:['python','/tmp/team cli.py'],args:['compile','/tmp/my agent'],argv:[]}),"python '/tmp/team cli.py' compile '/tmp/my agent'");
});

test('copyable commands quote shell syntax and preserve empty arguments',()=>{
  assert.equal(commandText(['acme','run','','$(touch x)',"it's"]),`acme run '' '$(touch x)' 'it'"'"'s'`);
});
