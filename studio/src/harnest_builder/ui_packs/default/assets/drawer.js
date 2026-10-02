/** Move the existing panel into a native dialog so drafts and selected tabs survive resizing. */
export function attachDrawer({panel,toggle,dialog,body,close,media=matchMedia('(max-width: 960px)')}) {
  const anchor=document.createComment('Studio panel position');panel.before(anchor);
  const events=new AbortController(),options={signal:events.signal};

  /** Start collapsed at narrow widths and restore the same panel on desktop. */
  function resize() {
    if(dialog.open)dialog.close();
    if(media.matches)body.append(panel);else anchor.after(panel);
    toggle.hidden=!media.matches;
    toggle.setAttribute('aria-expanded','false');
  }

  /** Use modal focus containment while leaving the workspace's state intact underneath. */
  function open() {
    if(!media.matches||dialog.open)return;
    panel.hidden=false;dialog.showModal();toggle.setAttribute('aria-expanded','true');
  }

  toggle.addEventListener('click',open,options);
  close.addEventListener('click',()=>dialog.close(),options);
  dialog.addEventListener('close',()=>{
    toggle.setAttribute('aria-expanded','false');
    if(media.matches)toggle.focus();
  },options);
  dialog.addEventListener('click',event=>{
    if(event.target!==dialog)return;
    const rect=dialog.getBoundingClientRect();
    if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();
  },options);
  media.addEventListener('change',resize,options);resize();
  return {open,dispose(){events.abort();if(dialog.open)dialog.close();anchor.after(panel);anchor.remove();}};
}
