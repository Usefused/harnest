import {apiURL,createState,createCommands,createSlots,frameState,appearanceSelection} from './host-api.js';

const state=createState(),commands=createCommands(),slots=createSlots(),guards=new Set(),cleanup=[];
const safe=new URLSearchParams(location.search).has('safe-ui');
let token='',catalog,preferenceKey='',currentAppearance;

/** Keep launch credentials in host transport, never in a pack's context. */
function authorize(value=location.href) {
  const url=new URL(value||location.href);
  if(url.origin!==location.origin)throw new Error('Use the launch URL for this Studio address.');
  const credential=new URLSearchParams(url.hash.slice(1)).get('token');
  if(credential)token=credential;
  if(new URLSearchParams(location.hash.slice(1)).has('token'))history.replaceState(null,'',location.pathname+location.search);
}

/** Establish the browser session before loading any pack code. */
async function request(path,method='GET',body) {
  const response=await fetch(apiURL('/api/'+path,location.origin),{method,headers:{...(token?{Authorization:'Bearer '+token}:{}),...(body!==undefined?{'Content-Type':'application/json'}:{})},...(body!==undefined?{body:JSON.stringify(body)}:{})});
  const value=await response.json();
  const detail=Array.isArray(value.detail)?value.detail.map(item=>item.msg).join('; '):value.detail;
  if(!response.ok){const error=new Error(typeof detail==='string'?detail:'Studio request failed');error.status=response.status;throw error;}
  if(path==='workspace')token='';
  return value;
}

/** Read only assets named by the validated server catalog. */
async function assetText(url) {
  const response=await fetch(url);
  if(!response.ok)throw new Error(`Could not load UI asset (${response.status}).`);
  return response.text();
}

/** Keep appearance per workspace and tolerate unavailable browser storage. */
function savedAppearance(value) {
  try {
    if(value!==undefined)localStorage.setItem(preferenceKey,JSON.stringify(value));
    return JSON.parse(localStorage.getItem(preferenceKey)||'{}')||{};
  } catch {return {};}
}

/** Apply validated literals through a nonce stylesheet, preserving the host CSP. */
function applyAppearance(saved,persist=false) {
  const selected=appearanceSelection(catalog,saved);
  let style=document.getElementById('studio-theme');
  if(!style){style=document.createElement('style');style.id='studio-theme';style.nonce=document.querySelector('meta[name="editor-style-nonce"]').content;document.head.append(style);}
  const visible=selected.order.filter(region=>!selected.hidden.includes(region));
  const widths={sidebar:'235px',workspace:'minmax(0,1fr)',inspector:'328px'};
  style.textContent=`:root{${Object.entries(selected.tokens).map(([key,value])=>`--${key}:${value};`).join('')}--studio-areas:"${visible.join(' ')}";--studio-columns:${visible.map(region=>widths[region]).join(' ')};}`;
  for(const region of document.querySelectorAll('[data-studio-region]'))region.hidden=selected.hidden.includes(region.dataset.studioRegion);
  currentAppearance={theme:selected.theme,layout:selected.layout};
  if(persist)savedAppearance(currentAppearance);
  return {...currentAppearance};
}

/** Give every trusted pack the same host API and scope relative asset access.
 * @param {import('../ui_sdk/index.js').Contribution} item
 * @returns {import('../ui_sdk/index.js').StudioContext}
 */
function context(item) {
  const moduleURL=new URL(item.base+item.entry,location.origin);
  return Object.freeze({
    version:1,request,authorize,state,commands,slots,
    /** Keep streaming requests on the same authenticated API boundary as JSON requests. */
    transport(path,options={}) {
      const headers=new Headers(options.headers);if(token)headers.set('Authorization','Bearer '+token);
      return fetch(apiURL(path,location.origin),{...options,headers});
    },
    navigation:{guard(handler){guards.add(handler);return ()=>guards.delete(handler);}},
    appearance:{catalog:()=>structuredClone({themes:catalog.themes,layouts:catalog.layouts,packs:catalog.packs}),current:()=>({...currentAppearance}),preview:selection=>applyAppearance(selection),save:selection=>applyAppearance(selection,true),reset:()=>applyAppearance({},true)},
    /** Mount trusted markup only from the activating pack's declared asset namespace. */
    async mountHTML(host,path) {
      const url=new URL(path,moduleURL);
      if(!url.href.startsWith(new URL(item.base,location.origin).href))throw new Error('UI asset escaped its pack');
      // Only explicitly trusted modules receive this API; isolated panels never parse HTML here.
      const template=document.createElement('template');template.innerHTML=await assetText(url);
      host.replaceChildren(template.content.cloneNode(true));
    },
  });
}

/** Isolate third-party HTML from cookies, DOM, navigation, network, and host commands. */
function mountFrame(item,host) {
  const frame=document.createElement('iframe');frame.className='studio-frame studio-contribution-frame';frame.title=item.title;
  frame.setAttribute('sandbox','allow-scripts');frame.referrerPolicy='no-referrer';
  let disconnect=()=>{};
  frame.addEventListener('load',()=>{
    disconnect();
    const channel=new MessageChannel();
    frame.contentWindow.postMessage({type:'studio:init',version:1},'*',[channel.port2]);
    const subscriptions=item.permissions.map(permission=>{
      const name=permission.slice('state.'.length);
      return state.subscribe(name,value=>channel.port1.postMessage({type:'studio:state',name,value:frameState(name,value)}));
    });
    // The bridge accepts no incoming commands or arbitrary API requests.
    disconnect=()=>{subscriptions.forEach(stop=>stop());channel.port1.close();};
  });
  frame.src=item.base+item.entry;host.append(frame);
  return ()=>{disconnect();frame.remove();};
}

/** Mount modules and framed views through one registry and deterministic slot ownership. */
async function mount(item) {
  const slot=item.slot==='shell'?document.getElementById('studio-root'):document.querySelector(`[data-studio-slot="${item.slot}"]`);
  if(!slot&&item.slot!=='service')throw new Error(`The selected shell does not provide slot: ${item.slot}`);
  const host=item.slot==='service'?null:document.createElement('div');
  if(host){
    host.dataset.studioContribution=item.id;slot.append(host);
    const detach=slots.attach(item.slot,host,item);
    if(typeof detach==='function')cleanup.push(detach);
  }
  if(item.mode==='frame'){cleanup.push(mountFrame(item,host));return;}
  const module=await import(item.base+item.entry);
  if(typeof module.activate!=='function')throw new Error(`${item.id} must export activate(studio, host)`);
  const dispose=await module.activate(context(item),host);
  if(typeof dispose==='function')cleanup.push(dispose);
}

/** Wait for styles before showing the shell to avoid a flash of unthemed controls. */
async function loadStyle(url) {
  await new Promise((resolve,reject)=>{
    const link=document.createElement('link');link.rel='stylesheet';link.href=url;link.dataset.studioStyle='true';
    link.onload=resolve;link.onerror=()=>reject(new Error('Could not load a UI stylesheet.'));document.head.append(link);
  });
}

/** Bootstrap authentication and composition without relying on default pack code. */
async function boot() {
  // Shell controls must not dispatch commands before their service owners finish mounting.
  const root=document.getElementById('studio-root');root.inert=true;
  authorize();
  const workspace=await request('workspace');preferenceKey='harnest-studio-ui:'+workspace.path;
  catalog=await request('ui'+(safe?'?safe=true':''));
  applyAppearance(safe?{}:savedAppearance());
  await Promise.all(catalog.styles.map(loadStyle));
  await mount(catalog.contributions.find(item=>item.slot==='shell'));
  for(const item of catalog.contributions.filter(item=>item.slot!=='shell'))await mount(item);
  applyAppearance(currentAppearance);
  root.inert=false;
  document.getElementById('studio-recovery').hidden=true;
}

/** Keep recovery available even when a pack fails halfway through mounting. */
function failed(error) {
  document.getElementById('studio-recovery').hidden=false;
  document.getElementById('studio-boot-status').textContent=error.message;
  document.getElementById('studio-connect').hidden=error.status!==401;
  for(const dispose of cleanup.splice(0).reverse()){
    try {dispose();}catch(problem){console.error('UI cleanup failed',problem);}
  }
  for(const style of document.querySelectorAll('[data-studio-style],#studio-theme'))style.remove();
  document.getElementById('studio-root').replaceChildren();
}

document.getElementById('studio-connect').addEventListener('submit',event=>{
  event.preventDefault();
  try {authorize(document.getElementById('studio-launch-url').value);document.getElementById('studio-launch-url').value='';boot().catch(failed);}catch(error){failed(error);}
});
window.addEventListener('beforeunload',event=>{if([...guards].some(guard=>!guard())){event.preventDefault();event.returnValue='';}});
boot().catch(failed);
