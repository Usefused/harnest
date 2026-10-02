import {basicSetup} from 'codemirror';
import {EditorState} from '@codemirror/state';
import {EditorView,keymap} from '@codemirror/view';
import {indentWithTab} from '@codemirror/commands';
import {python} from '@codemirror/lang-python';
import {json} from '@codemirror/lang-json';
import {yaml} from '@codemirror/lang-yaml';
import {javascript} from '@codemirror/lang-javascript';
import {markdown} from '@codemirror/lang-markdown';
import {StreamLanguage} from '@codemirror/language';
import {toml} from '@codemirror/legacy-modes/mode/toml';
import {oneDark} from '@codemirror/theme-one-dark';
import {lintGutter,lintKeymap,setDiagnostics,openLintPanel} from '@codemirror/lint';
import {editorDiagnostics} from './positions.mjs';

/** Select local parsers without downloading language code at runtime. */
function language(path) {
  const extension=path.split('.').at(-1);
  const parsers={py:python,json,yaml,yml:yaml,js:javascript,md:markdown,mdx:markdown,toml:()=>StreamLanguage.define(toml)};
  return parsers[extension]?.()||[];
}

/** Keep the native save contract while providing structured editing and local draft checks. */
export class StudioEditor {
  constructor({host,textarea,status,request,changed}) {
    Object.assign(this,{textarea,status,request,changed,generation:0,identity:'',path:'',project:null,syncing:false});
    this.view=new EditorView({parent:host,state:this.state('',true)});
    status.addEventListener('click',()=>{openLintPanel(this.view);this.view.focus();});
  }

  /** Recreate editor state only when changing files so undo never crosses project boundaries. */
  state(text,disabled) {
    return EditorState.create({doc:text,extensions:[basicSetup,oneDark,language(this.path),lintGutter(),
      keymap.of([indentWithTab,...lintKeymap]),EditorState.readOnly.of(disabled),EditorView.editable.of(!disabled),
      EditorView.cspNonce.of(document.querySelector('meta[name="editor-style-nonce"]').content),
      EditorView.contentAttributes.of({'aria-label':'Source code editor','spellcheck':'false'}),
      EditorView.theme({'&':{height:'100%',backgroundColor:'var(--editor-bg)'},'.cm-scroller':{fontFamily:'var(--font-code)',fontSize:'var(--editor-size)',lineHeight:'1.8'},'.cm-gutters':{backgroundColor:'var(--editor-gutter)'},'.cm-content':{padding:'18px 0'},'.cm-line':{padding:'0 16px'}}),
      EditorView.updateListener.of(update=>{
        if(!update.docChanged||this.syncing)return;
        this.textarea.value=update.state.doc.toString();this.changed();this.schedule();
      })]});
  }

  /** Mirror programmatic opens/reloads without replacing user edits or resetting undo on save. */
  sync(text,path,project,disabled) {
    const identity=JSON.stringify([project,path,disabled]), replace=identity!==this.identity;
    const changed=text!==this.view.state.doc.toString();
    this.path=path||'';this.project=project;this.identity=identity;
    this.syncing=true;
    try {
      if(replace)this.view.setState(this.state(text,disabled));
      else if(changed)this.view.dispatch({changes:{from:0,to:this.view.state.doc.length,insert:text}});
    } finally {this.syncing=false;}
    if(replace||changed)this.schedule();
  }

  /** Invalidate pending responses immediately; lint only after a short typing pause. */
  schedule() {
    clearTimeout(this.timer);
    const generation=++this.generation;
    this.status.disabled=true;this.status.title='';
    if(!this.path.endsWith('.py')||!this.project){this.status.textContent='';return;}
    this.status.textContent='Checking Python…';
    queueMicrotask(()=>{if(generation===this.generation)this.view.dispatch(setDiagnostics(this.view.state,[]));});
    this.timer=setTimeout(()=>this.lint(generation),400);
  }

  /** Ignore replies for a previous draft or file, including drafts with identical source. */
  async lint(generation) {
    const text=this.view.state.doc.toString(), path=this.path, project=this.project;
    try {
      const result=await this.request({project,path,text});
      if(generation!==this.generation)return;
      this.view.dispatch(setDiagnostics(this.view.state,editorDiagnostics(text,result.diagnostics)));
      const count=result.diagnostics.length;
      this.status.textContent=count?`${count}${result.truncated?'+':''} issue${count===1?'':'s'}`:'No Python issues';
      this.status.disabled=!count;this.status.title=count?'Show Python diagnostics':'Ruff correctness checks passed';
    } catch(error) {
      if(generation!==this.generation)return;
      this.status.textContent='Lint unavailable';this.status.title=error.message;
    }
  }

  /** Focus the visible editor rather than its hidden save buffer. */
  focus(){this.view.focus();}
}
