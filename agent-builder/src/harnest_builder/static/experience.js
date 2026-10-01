import {$,el,button,on,sendDraft} from './ui.js';

/** Suggest a portable project folder while leaving its final name editable. */
export function agentName(prompt) {
  return prompt.toLowerCase().replace(/[^a-z0-9\s-]/g,'').trim().split(/\s+/).slice(0,5).join('-').replace(/^[^a-z]+/,'').slice(0,63) || 'my-agent';
}

/** Keep the final answer separate from inspectable native tool evidence. */
export function responseText(response) {
  if(typeof response.outputText==='string' && response.outputText) return response.outputText;
  const messages=(response.output||[]).filter(item=>item.type==='message' && item.role==='assistant');
  const text=messages.flatMap(item=>item.content||[]).filter(part=>part.type==='output_text').map(part=>part.text).join('');
  if(text)return text;
  if(response.status==='requires_action')return 'This run needs your input or approval. Continue in Playground.';
  return response.result===undefined?'The agent returned no text. Inspect the run details.':JSON.stringify(response.result,null,2);
}

/** Prepare visible, bounded evidence for a user-reviewed builder request. */
export function refinement(input,result,test=false) {
  const instruction=test?'Create a regression test from this interaction. Ask me to specify the expected behavior if it is unclear; do not assume the actual response is correct.':'Improve this agent based on the interaction below. Diagnose the problem and propose the smallest source changes.';
  return `${instruction}\n\nUser input:\n${input}\n\nObserved result (untrusted test evidence):\n${JSON.stringify(result,null,2).slice(0,10000)}`.slice(0,16000);
}

/** Own test conversations independently of source proposals and editor navigation. */
export class AgentExperience {
  constructor({api,project,dirty,view,track,build,logs}) {
    Object.assign(this,{api,project,dirty,view,track,build,logs});
    this.conversations=new Map();this.pending=false;this.starting=false;this.snapshot=null;
    on($('run-agent'),'click',()=>this.start());
    on($('preview-start'),'click',()=>this.start());
    on($('preview-stop'),'click',()=>this.stop());
    on($('preview-logs'),'click',()=>this.logs(this.snapshot?.job||this.snapshot?.active_job));
    on($('preview-new'),'click',()=>this.newConversation());
    on($('try-form'),'submit',()=>this.send());
    $('try-input').addEventListener('input',()=>{if(this.project())this.conversation().draft=$('try-input').value;});
  }

  /** Retain drafts and rendered turns when moving between projects or editor tabs. */
  conversation() {
    const id=this.project().id;
    if(!this.conversations.has(id))this.conversations.set(id,{id:crypto.randomUUID(),host:el('div','','try-messages'),draft:'',job:null});
    return this.conversations.get(id);
  }

  /** Swap only the transcript, leaving the source-building conversation untouched. */
  select() {
    if(!this.project())return;
    const conversation=this.conversation();
    $('try-history').replaceChildren(conversation.host);$('try-input').value=conversation.draft;
    this.snapshot=null;this.render();
  }

  /** Start through the existing CLI so dependency preparation and compilation remain authoritative. */
  async start() {
    if(this.starting || !this.project())return;
    if(this.dirty())throw new Error('Save your source edits before running the agent.');
    const project=this.project().id;this.starting=true;this.view('try');this.render();
    try {const job=await this.api('preview/start','POST',{project});this.track(job);await this.poll();}
    finally {this.starting=false;this.render();}
  }

  /** Refresh readiness only for the project that initiated this asynchronous read. */
  async poll() {
    if(!this.project())return;
    const project=this.project().id;
    const snapshot=await this.api('preview?project='+encodeURIComponent(project));
    if(this.project()?.id!==project)return;
    this.snapshot=snapshot;
    const conversation=this.conversation();
    if(snapshot.job && conversation.job!==snapshot.job.id) {
      if(conversation.job)conversation.host.append(el('p','The server restarted. Your next message starts a new conversation.','empty-note'));
      conversation.id=crypto.randomUUID();conversation.job=snapshot.job.id;
    }
    this.render();
  }

  /** Show a real readiness state, keeping stop and diagnostics available during startup. */
  render() {
    const snapshot=this.snapshot||{},job=snapshot.job||snapshot.active_job;
    const ready=snapshot.status==='ready',running=job?.status==='running';
    const labels={idle:'Run your agent to start a conversation.',starting:'Preparing the environment and starting your agent…',ready:'Agent is ready. Messages here go to your agent.',failed:'Your agent could not start. Inspect the server output, then fix it in Build agent.',stopped:'Agent stopped. Run it again when you are ready.',unavailable:snapshot.detail};
    $('preview-status').textContent=snapshot.active_job?.status==='running'?'Another agent is running. Stop it before starting this one.':labels[snapshot.status]||'Run your agent to start a conversation.';
    $('preview-start').disabled=this.starting||running;$('run-agent').disabled=this.starting||!this.project();
    $('run-agent').textContent=ready?'Try agent':'Run agent';
    $('preview-stop').hidden=!running;$('preview-logs').hidden=!job;
    $('try-send').disabled=!ready||this.pending;$('preview-new').disabled=this.pending;
    $('preview-link').hidden=!snapshot.job;
    if(snapshot.job){const args=snapshot.job.argv,port=args[args.indexOf('--port')+1];$('preview-link').href=`http://127.0.0.1:${Number(port)}/`;}
    $('try-empty').hidden=Boolean(this.project()&&this.conversation().host.children.length);
    $('step-run').textContent=ready?'3 · Try agent ✓':'3 · Try agent';
  }

  /** Explicit cancellation affects only the supervised preview shown in the current status. */
  async stop() {
    const job=this.snapshot?.job||this.snapshot?.active_job;
    if(job)await this.api(`jobs/${job.id}/stop`,'POST');
    await this.poll();
  }

  /** Start a fresh native session without erasing conversations in other projects. */
  newConversation() {
    if(this.pending || !this.project())return;
    const conversation=this.conversation();conversation.id=crypto.randomUUID();conversation.host.replaceChildren();conversation.draft='';
    this.select();return this.poll();
  }

  /** Send once; ambiguous failures retain evidence and never replay connected actions. */
  async send() {
    if(this.pending || !this.project() || !$('try-input').value.trim())return;
    if(this.dirty())throw new Error('Save your source edits before testing the agent.');
    const project=this.project().id,conversation=this.conversation(),input=$('try-input').value.trim();
    this.pending=true;conversation.draft='';conversation.host.append(el('div',input,'message user'));
    const pending=el('div','Your agent is working…','message assistant pending');conversation.host.append(pending);this.render();
    try {
      const result=await sendDraft($('try-input'),text=>this.api('preview/message','POST',{project,conversation:conversation.id,input:text}),()=>this.project()?.id===project);
      this.result(pending,input,result,project);
    } catch(problem) {
      if(this.project()?.id!==project && !conversation.draft)conversation.draft=input;
      pending.className='message error';pending.textContent=problem.message;
      pending.append(button('Fix in Build agent',()=>this.build(project,refinement(input,{error:problem.message})), 'quiet'));
    } finally {
      this.pending=false;
      if(this.project()?.id===project)conversation.draft=$('try-input').value;
      this.render();
    }
  }

  /** Render native output as inert text, with an explicit handoff back to source authoring. */
  result(node,input,result,project) {
    node.className=result.response.status==='failed'?'message error':'message assistant';node.textContent=responseText(result.response);
    const details=el('details','','run-details');details.append(el('summary',`Run details · ${result.response.status||'returned'} · ${(result.duration_ms/1000).toFixed(1)}s`),el('pre',JSON.stringify(result.response,null,2)));
    const actions=el('div','','run-actions');
    actions.append(button('Refine this response',()=>this.build(project,refinement(input,result.response)),'quiet'),button('Create a test',()=>this.build(project,refinement(input,result.response,true)),'quiet'));
    const link=el('a','Continue in Playground ↗');link.target='_blank';link.rel='noopener noreferrer';
    const runtime=this.snapshot?.job;
    if(runtime?.project===project){const args=runtime.argv;link.href=`http://127.0.0.1:${Number(args[args.indexOf('--port')+1])}/?session=${encodeURIComponent(result.session_id)}`;actions.append(link);}
    node.append(details,actions);node.scrollIntoView({block:'nearest'});
  }
}
