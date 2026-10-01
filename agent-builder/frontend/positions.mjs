/** Convert Ruff's one-based Unicode columns into CodeMirror's UTF-16 offsets. */
export function sourceOffset(text, location) {
  const lines=text.split('\n'), row=Math.max(0,Math.min(lines.length-1,(location?.row||1)-1));
  const prefix=lines.slice(0,row).reduce((length,line)=>length+line.length+1,0);
  return prefix+[...lines[row]].slice(0,Math.max(0,(location?.column||1)-1)).join('').length;
}

/** Clamp incomplete syntax ranges, including errors at the end of a draft. */
export function editorDiagnostics(text, diagnostics) {
  return diagnostics.map(item=>{
    const from=sourceOffset(text,item.start), end=sourceOffset(text,item.end);
    return {from,to:Math.max(from,end),severity:item.severity,message:`${item.code}: ${item.message}`,source:'Ruff'};
  });
}
