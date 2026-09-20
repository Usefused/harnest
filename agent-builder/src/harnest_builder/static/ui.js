/** Small inert DOM helpers shared by the standalone builder screens. */
export const $ = id => document.getElementById(id);

/** Keep filenames, provider output, and source text out of HTML parsing. */
export function el(tag, text = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = text; node.className = className;
  return node;
}

/** Surface asynchronous failures instead of leaving dead controls in the interface. */
export function on(node, event, action) {
  node.addEventListener(event, value => {
    // Cancel form navigation before deferred asynchronous validation runs.
    if(event === "submit") value.preventDefault();
    Promise.resolve().then(() => action(value)).catch(error);
  });
}

/** Use one error surface for rejected commands, conflicts, and disconnected requests. */
export function error(failure) {
  $("toast").textContent = failure.message || String(failure);
  $("toast").hidden = false;
  clearTimeout(error.timer);
  error.timer = setTimeout(() => { $("toast").hidden = true; }, 8500);
  status(failure.message || String(failure));
}

/** Announce state changes without stealing focus from ongoing editing. */
export function status(message) { $("status").textContent = message; }

/** Produce a native keyboard-operable action with shared styling. */
export function button(label, action, className = "button secondary") {
  const node = el("button", label, className); node.type = "button";
  on(node, "click", action); return node;
}

/** Associate every modal field with its label and native validation contract. */
export function field(host, label, value = "", options = {}) {
  const wrapper = el("label", "", "dialog-field");
  wrapper.append(el("span", label));
  const input = el(options.multiline ? "textarea" : "input");
  Object.assign(input, {value, ...options});
  wrapper.append(input);
  if (options.hint) wrapper.append(el("small", options.hint));
  host.append(wrapper); return input;
}

/** Keep native select elements as the value and accessibility source of truth. */
export function select(host, label, choices, value) {
  const wrapper = el("label", "", "dialog-field"); wrapper.append(el("span", label));
  const input = el("select");
  for (const [key, title] of choices) { const option = el("option", title); option.value = key; input.append(option); }
  if (value !== undefined) input.value = value;
  wrapper.append(input); host.append(wrapper); return input;
}

/** Native dialogs trap focus; rejected submissions keep the user's completed form intact. */
export function modal(title, description, label, submit, wide = false) {
  const dialog = $("dialog"); dialog.replaceChildren(); dialog.className = wide ? "wide" : "";
  const heading = el("h2", title); heading.id = "dialog-title";
  const form = el("form", "", "dialog-form"), content = el("div", "", "dialog-form");
  const failure = el("p", "", "dialog-error"); failure.setAttribute("role", "alert");
  const actions = el("div", "", "dialog-actions");
  const save = el("button", label, "button primary"); save.type = "submit";
  actions.append(button("Cancel", () => dialog.close()), save);
  form.append(content, failure, actions); dialog.append(heading, el("p", description, "dialog-description"), form);
  form.addEventListener("submit", async event => {
    event.preventDefault(); save.disabled = true; failure.textContent = "";
    try { const close = await submit(); if(close !== false) dialog.close(); }
    catch (problem) { failure.textContent = problem.message || String(problem); }
    finally { save.disabled = false; }
  });
  if (!dialog.open) dialog.showModal();
  setTimeout(() => content.querySelector("input, select, textarea, button")?.focus(), 0);
  return content;
}

/** Persist optional UI preferences without making blocked browser storage fatal. */
export function preference(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem("harnest-builder:" + key) || "null");
    localStorage.setItem("harnest-builder:" + key, JSON.stringify(value));
  } catch (_) { /* Private browsing can deny optional layout persistence. */ }
  return null;
}

/** Clear a submitted draft immediately; restore failures only if the composer is untouched. */
export async function sendDraft(input, send, canRestore = () => true) {
  const draft=input.value;
  let edited=false;
  const changed=()=>{edited=true;};
  input.value="";
  input.addEventListener("input",changed);
  try { return await send(draft.trim()); }
  catch(problem) {
    // A new draft or project switch owns the composer, even if the new draft was erased.
    if(!edited && input.value==="" && canRestore()) input.value=draft;
    throw problem;
  } finally { input.removeEventListener("input",changed); }
}


/** Preserve line endings so newline-only changes remain reviewable. */
function lines(text) { return text.match(/[^\n]*\n|[^\n]+$/g)||[]; }

/** Match the changed region with bounded memory; large rewrites remain exact replacements. */
export function diffLines(before,after) {
  const old=lines(before),next=lines(after),ops=[];
  let prefix=0,suffix=0;
  while(prefix<old.length && prefix<next.length && old[prefix]===next[prefix])prefix++;
  while(suffix<old.length-prefix && suffix<next.length-prefix && old[old.length-1-suffix]===next[next.length-1-suffix])suffix++;
  const left=old.slice(prefix,old.length-suffix),right=next.slice(prefix,next.length-suffix);
  const append=(type,text)=>ops.push({type,text});
  old.slice(0,prefix).forEach(text=>append('same',text));
  const coarse=left.length*right.length>1_000_000;
  if(coarse) {
    left.forEach(text=>append('removed',text));right.forEach(text=>append('added',text));
  } else {
    const width=right.length+1,table=new Uint32Array((left.length+1)*width);
    for(let i=left.length-1;i>=0;i--) for(let j=right.length-1;j>=0;j--) {
      table[i*width+j]=left[i]===right[j]?1+table[(i+1)*width+j+1]:Math.max(table[(i+1)*width+j],table[i*width+j+1]);
    }
    let i=0,j=0;
    while(i<left.length || j<right.length) {
      if(i<left.length && j<right.length && left[i]===right[j]){append('same',left[i++]);j++;}
      else if(i<left.length && (j===right.length || table[(i+1)*width+j]>=table[i*width+j+1]))append('removed',left[i++]);
      else append('added',right[j++]);
    }
  }
  old.slice(old.length-suffix).forEach(text=>append('same',text));
  let oldLine=0,newLine=0;
  return {coarse,rows:ops.map(row=>({...row,oldLine:row.type==='added'?null:++oldLine,newLine:row.type==='removed'?null:++newLine}))};
}

/** Keep nearby unchanged lines around each edit, combining overlapping context windows. */
export function contextRows(rows,context=3) {
  const visible=new Set();
  rows.forEach((row,index)=>{if(row.type!=='same')for(let i=Math.max(0,index-context);i<=Math.min(rows.length-1,index+context);i++)visible.add(i);});
  const result=[];
  for(let i=0;i<rows.length;) {
    if(visible.has(i)){result.push(rows[i++]);continue;}
    let count=0;while(i<rows.length&&!visible.has(i)){i++;count++;}
    result.push({type:'gap',count});
  }
  return result;
}

/** Render source as inert text with line numbers, explicit signs, and highlighted changes. */
function diffRow(row) {
  const tr=el('tr','','diff-'+row.type);
  if(row.type==='gap') {const cell=el('td',`${row.count} unchanged lines hidden`);cell.colSpan=4;tr.append(cell);return tr;}
  const sign=row.type==='added'?'+':row.type==='removed'?'−':' ';
  const marker=el('td',sign,'diff-sign');marker.setAttribute('aria-label',row.type==='same'?'Unchanged':row.type==='added'?'Added':'Removed');
  const source=el('td','','diff-source');source.append(el('code',row.text.replace(/\r?\n$/,'')));
  if(!row.text.endsWith('\n'))source.append(el('span','No newline at end of file','diff-ending'));
  else if(row.text.endsWith('\r\n') && row.type!=='same')source.append(el('span','CRLF','diff-ending'));
  tr.append(el('td',row.oldLine??'','diff-number'),el('td',row.newLine??'','diff-number'),marker,source);return tr;
}

/** Show focused changes by default, retaining an option to inspect every unchanged line. */
export function renderDiff(host,file) {
  host.replaceChildren();
  const {rows,coarse}=diffLines(file.before||'',file.text);
  const added=rows.filter(row=>row.type==='added').length,removed=rows.filter(row=>row.type==='removed').length;
  const summary=el('div','','diff-summary');
  summary.append(el('strong',file.revision?'Existing file':'New file'),el('span',`+${added} added`,'diff-added-count'),el('span',`−${removed} removed`,'diff-removed-count'));
  host.append(summary);
  if(!added&&!removed)host.append(el('p','No content changes.','empty-note'));
  if(coarse)host.append(el('p','Large rewrite: the replaced section is shown in full.','empty-note'));
  const scroll=el('div','','diff-scroll');scroll.tabIndex=0;scroll.setAttribute('aria-label',`Changes to ${file.path}`);
  const table=el('table','','diff-table'),head=el('thead'),header=el('tr'),body=el('tbody');
  for(const label of ['Old','New','+/−','Source'])header.append(el('th',label));
  head.append(header);table.append(head,body);scroll.append(table);host.append(scroll);
  let full=false;
  const draw=()=>{body.replaceChildren(...(full?rows:contextRows(rows)).map(diffRow));};
  const toggle=button('Show full file',()=>{full=!full;toggle.textContent=full?'Show changes only':'Show full file';toggle.setAttribute('aria-pressed',String(full));draw();},'quiet');
  toggle.setAttribute('aria-pressed','false');summary.append(toggle);draw();
}

const commandDisplays=new WeakMap();

/** Copy the exact source text without copying controls or visual line wrapping. */
export async function copyCommandText(text) {
  await navigator.clipboard.writeText(text);
  status("Command output copied.");
}

/** Return the visible snapshot, including when incoming output is deferred during selection. */
export function commandOutputText(host) { return commandDisplays.get(host)?.text||""; }

/** Preserve stable output nodes and defer streaming updates while text is being selected. */
export function renderCommandOutput(host,text,jobId) {
  const previous=commandDisplays.get(host),selection=document.getSelection();
  const sameJob=Boolean(previous && previous.jobId===jobId);
  if(sameJob && previous.text===text)return;
  if(sameJob && selection && !selection.isCollapsed && (host.contains(selection.anchorNode)||host.contains(selection.focusNode)))return;
  const follow=host.scrollHeight-host.scrollTop-host.clientHeight<50;
  const next=text.split("\n"),old=sameJob?previous.lines:[];
  if(!sameJob)host.replaceChildren();
  next.forEach((line,index)=>{
    if(old[index]===line)return;
    const row=el("div","","output-line"),source=el("code",line,"output-text");
    const copy=button("Copy",()=>copyCommandText(line),"copy-output-line");
    copy.setAttribute("aria-label",`Copy output line ${index+1}`);copy.title="Copy line";
    row.append(source,copy);
    if(host.children[index])host.children[index].replaceWith(row);else host.append(row);
  });
  while(host.children.length>next.length)host.lastElementChild.remove();
  commandDisplays.set(host,{jobId,text,lines:next});
  if(follow && (!selection || selection.isCollapsed))host.scrollTop=host.scrollHeight;
}
