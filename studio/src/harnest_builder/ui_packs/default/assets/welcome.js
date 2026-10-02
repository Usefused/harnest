import {$,on} from './ui.js';

/** Reuse the same commands and state available to replacement UI modules. */
export async function activate(studio,host) {
  await studio.mountHTML(host,'welcome.html');
  on($('idea-form'),'submit',()=>studio.commands.execute('studio.project.create',{prompt:$('agent-idea').value}));
  return studio.state.subscribe('connection',value=>{$('welcome-create').disabled=!value.ready;});
}
