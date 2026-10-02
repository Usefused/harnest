import {el,button} from './ui.js';

/** Keep settings in the default pack using only the public host contract. */
export function activate(studio) {
  return studio.commands.register('studio.appearance',()=>showAppearance(studio));
}

/** Preview immediately and restore the previous selection when users cancel. */
function showAppearance(studio) {
  const catalog=studio.appearance.catalog(),original=studio.appearance.current();
  let saved=false,theme,layout;
  const selection=()=>({theme:theme.value,layout:layout.value});
  const {dialog,form}=appearanceDialog(()=>{studio.appearance.save(selection());saved=true;});
  theme=picker(form,'Theme',catalog.themes,original.theme);
  layout=picker(form,'Layout',catalog.layouts,original.layout);
  for(const picker of [theme,layout])picker.addEventListener('change',()=>studio.appearance.preview(selection()));
  dialog.addEventListener('close',()=>{if(!saved)studio.appearance.preview(original);dialog.remove();},{once:true});
  form.append(button('Reset to Fused defaults',()=>{const reset=studio.appearance.preview({});theme.value=reset.theme;layout.value=reset.layout;}));
  form.append(el('p','Loaded UI packs','eyebrow'));
  for(const pack of catalog.packs)form.append(el('p',`${pack.id} · ${pack.version} · ${pack.trusted?'trusted code':'isolated panels and themes'}`,'small muted'));
  const link=el('a','Restore default UI');link.href='/?safe-ui=1';form.append(link);
  dialog.showModal();theme.focus();
}

/** Own the settings dialog so reusing this service never requires default-shell DOM IDs. */
function appearanceDialog(save) {
  const dialog=el('dialog'),title=el('h2','Studio appearance');title.id='studio-appearance-title';
  dialog.setAttribute('aria-labelledby',title.id);
  const outer=el('form','','dialog-form'),form=el('div','','dialog-form'),actions=el('div','','dialog-actions');
  const submit=el('button','Save','button primary');submit.type='submit';
  actions.append(button('Cancel',()=>dialog.close()),submit);outer.append(form,actions);
  outer.addEventListener('submit',event=>{event.preventDefault();save();dialog.close();});
  dialog.append(title,el('p','Choose a theme and workspace layout from your loaded packs.','dialog-description'),outer);
  document.body.append(dialog);
  return {dialog,form};
}

/** Preserve native selects as the shared keyboard and dropdown contract. */
function picker(form,label,items,value) {
  const wrapper=el('label','','dialog-field'),select=el('select');wrapper.append(el('span',label),select);
  select.setAttribute('aria-label',label);
  for(const item of items){const option=el('option',item.title);option.value=item.id;select.append(option);}
  select.value=value;form.append(wrapper);return select;
}
