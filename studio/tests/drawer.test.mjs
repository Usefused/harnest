import assert from 'node:assert/strict';
import test from 'node:test';
import {attachDrawer} from '../src/harnest_builder/ui_packs/default/assets/drawer.js';

/** Model node movement and native dialog events without substituting a new panel instance. */
class Element extends EventTarget {
  constructor(){super();this.attributes={};this.open=false;}
  before(anchor){anchor.parent=this.parent;}
  after(node){node.parent=this.parent;}
  append(node){node.parent=this;}
  remove(){this.parent=null;}
  setAttribute(name,value){this.attributes[name]=value;}
  showModal(){this.open=true;}
  close(){this.open=false;this.dispatchEvent(new Event('close'));}
  focus(){this.focused=true;}
}

test('narrow panels start collapsed, preserve drafts through open/close, and return to the desktop column',()=>{
  const previous=globalThis.document;
  globalThis.document={createComment:()=>new Element()};
  const desktop={},panel=new Element(),toggle=new Element(),dialog=new Element(),body=new Element(),close=new Element(),media=new EventTarget();
  panel.parent=desktop;panel.draft='Keep my unsent prompt';media.matches=true;
  try {
    const drawer=attachDrawer({panel,toggle,dialog,body,close,media});
    assert.equal(panel.parent,body);assert.equal(dialog.open,false);assert.equal(toggle.hidden,false);
    toggle.dispatchEvent(new Event('click'));
    assert.equal(dialog.open,true);assert.equal(toggle.attributes['aria-expanded'],'true');
    close.dispatchEvent(new Event('click'));
    assert.equal(dialog.open,false);assert.equal(toggle.attributes['aria-expanded'],'false');assert.equal(toggle.focused,true);
    drawer.open();media.matches=false;media.dispatchEvent(new Event('change'));
    assert.equal(dialog.open,false);assert.equal(panel.parent,desktop);assert.equal(toggle.hidden,true);
    media.matches=true;media.dispatchEvent(new Event('change'));
    assert.equal(dialog.open,false);assert.equal(panel.parent,body);assert.equal(panel.draft,'Keep my unsent prompt');
    drawer.dispose();assert.equal(panel.parent,desktop);
    media.dispatchEvent(new Event('change'));assert.equal(panel.parent,desktop);
  } finally {globalThis.document=previous;}
});
