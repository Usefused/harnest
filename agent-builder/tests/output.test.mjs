import assert from 'node:assert/strict';
import test from 'node:test';
import {renderCommandOutput,commandOutputText} from '../src/harnest_builder/static/ui.js';

function element() {
  return {children:[],textContent:'',scrollHeight:100,scrollTop:0,clientHeight:100,
    append(...items){for(const item of items){item.parent=this;this.children.push(item);}},
    replaceChildren(...items){this.children=[];this.append(...items);},
    replaceWith(item){const index=this.parent.children.indexOf(this);item.parent=this.parent;this.parent.children[index]=item;},
    remove(){this.parent.children.splice(this.parent.children.indexOf(this),1);},
    contains(node){return node===this||this.children.some(child=>child.contains(node));},
    get lastElementChild(){return this.children.at(-1);},
    setAttribute(){},addEventListener(){},
  };
}

test('polling preserves line nodes, selections and the exact visible output',()=>{
  const original=globalThis.document;
  let selection={isCollapsed:true};
  globalThis.document={createElement:element,getSelection:()=>selection};
  try {
    const host=element();
    renderCommandOutput(host,'No commands yet',undefined);
    renderCommandOutput(host,'$ test\n\nfirst\n','job1');
    const first=host.children[0],line=host.children[2];
    renderCommandOutput(host,'$ test\n\nfirst\n','job1');
    assert.equal(host.children[0],first);assert.equal(host.children[2],line);
    selection={isCollapsed:false,anchorNode:line.children[0],focusNode:line.children[0]};
    renderCommandOutput(host,'$ test\n\nfirst\nsecond\n','job1');
    assert.equal(host.children.length,4);
    assert.equal(commandOutputText(host),'$ test\n\nfirst\n');
    selection={isCollapsed:true};
    renderCommandOutput(host,'$ test\n\nfirst\nsecond\n','job1');
    assert.equal(host.children[0],first);assert.equal(host.children[2],line);
    assert.equal(host.children.length,5);
    assert.equal(commandOutputText(host),'$ test\n\nfirst\nsecond\n');
    renderCommandOutput(host,'different','job2');
    assert.equal(host.children.length,1);assert.equal(host.children[0].children[0].textContent,'different');
  } finally {globalThis.document=original;}
});

test('partial streaming lines update without replacing preceding lines or scrolling back',()=>{
  const original=globalThis.document;
  globalThis.document={createElement:element,getSelection:()=>({isCollapsed:true})};
  try {
    const host=element();
    renderCommandOutput(host,'done\npar','job');const first=host.children[0];
    host.scrollHeight=1000;host.scrollTop=25;
    renderCommandOutput(host,'done\npartial','job');
    assert.equal(host.children[0],first);
    assert.equal(host.children[1].children[0].textContent,'partial');
    assert.equal(host.scrollTop,25);
  } finally {globalThis.document=original;}
});

test('clipboard receives exact output text, without controls or wrapping',async()=>{
  const originalDocument=globalThis.document,descriptor=Object.getOwnPropertyDescriptor(globalThis,'navigator');
  const messages={},copied=[];
  globalThis.document={getElementById:()=>messages};
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{clipboard:{writeText:async text=>copied.push(text)}}});
  try {
    const {copyCommandText}=await import('../src/harnest_builder/static/ui.js');
    const line='  exact <source> text\twith spacing';
    await copyCommandText(line);
    await copyCommandText('$ command\n\n'+line+'\n');
    assert.deepEqual(copied,[line,'$ command\n\n'+line+'\n']);
    assert.equal(messages.textContent,'Command output copied.');
  } finally {
    globalThis.document=originalDocument;
    if(descriptor)Object.defineProperty(globalThis,'navigator',descriptor);else delete globalThis.navigator;
  }
});
