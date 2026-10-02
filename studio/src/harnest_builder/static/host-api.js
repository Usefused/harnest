/** Clone state so consumers cannot mutate another contribution's snapshot. */
export function createState(onError=error=>console.error('UI state subscriber failed',error)) {
  const values=new Map(), listeners=new Map();
  /** A failing extension must not prevent other views from receiving state. */
  function notify(listener,value) {
    try {listener(structuredClone(value));}catch(error){onError(error);}
  }
  return Object.freeze({
    /** Publish copies so each subscriber owns its received data. */
    publish(name,value) {
      values.set(name,structuredClone(value));
      for(const listener of listeners.get(name)||[])notify(listener,value);
    },
    /** Replay the latest value and return a subscription-scoped disposer. */
    subscribe(name,listener) {
      if(!listeners.has(name))listeners.set(name,new Set());
      listeners.get(name).add(listener);
      if(values.has(name))notify(listener,values.get(name));
      return ()=>listeners.get(name).delete(listener);
    },
  });
}

/** Normalize API paths without rejecting legitimate filenames inside query parameters. */
export function apiURL(path,origin) {
  const url=new URL(path,origin);
  if(url.origin!==origin||!url.pathname.startsWith('/api/')||url.hash)throw new Error('Invalid Studio API path');
  return url;
}

/** Commands have one owner; later packs cannot silently intercept existing actions. */
export function createCommands() {
  const commands=new Map();
  return Object.freeze({
    /** Reserve a command until its owner disposes it. */
    register(name,handler) {
      if(commands.has(name))throw new Error(`Command already registered: ${name}`);
      commands.set(name,handler);
      return ()=>commands.delete(name);
    },
    /** Preserve arguments and asynchronous results across the command boundary. */
    execute(name,...args) {
      if(!commands.has(name))throw new Error(`Unknown Studio command: ${name}`);
      return commands.get(name)(...args);
    },
  });
}

/** Let the selected shell own presentation while all views use the same slot contract. */
export function createSlots() {
  const adapters=new Map();
  return Object.freeze({
    /** Require one presentation adapter per slot. */
    register(name,adapter) {
      if(adapters.has(name))throw new Error(`Slot already registered: ${name}`);
      adapters.set(name,adapter);
      return ()=>adapters.delete(name);
    },
    attach(name,host,item) {return adapters.get(name)?.(host,item);},
  });
}

/** Share only explicitly granted, low-information state with an isolated panel. */
export function frameState(name,value) {
  if(value==null)return null;
  if(name==='connection')return {ready:value.ready===true};
  if(name==='project')return {id:String(value.id||''),name:String(value.name||''),framework:String(value.framework||'')};
  throw new Error('Unsupported frame state');
}

/** Fall back when saved appearance preferences reference an unavailable pack. */
export function appearanceSelection(catalog,saved={}) {
  const theme=catalog.themes.find(item=>item.id===saved.theme)||catalog.themes.find(item=>item.id==='fused-studio/default');
  const layout=catalog.layouts.find(item=>item.id===saved.layout)||catalog.layouts.find(item=>item.id==='fused-studio/default');
  const defaults=catalog.themes.find(item=>item.id==='fused-studio/default');
  return {theme:theme.id,layout:layout.id,tokens:{...defaults.tokens,...theme.tokens},order:layout.order,hidden:layout.hidden};
}
