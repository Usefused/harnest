/** Decode AG-UI SSE across arbitrary UTF-8/network chunk boundaries. */
export async function readEvents(response, receive) {
  if(!response.ok) {
    const body=await response.json();
    throw new Error(typeof body.detail==="string"?body.detail:`Builder request failed (${response.status}).`);
  }
  const reader=response.body.getReader(),decoder=new TextDecoder();
  let buffer="";
  try {
    while(true) {
      const {done,value}=await reader.read();
      buffer+=decoder.decode(value,{stream:!done});
      buffer=buffer.replace(/\r\n/g,"\n");
      let boundary;
      while((boundary=buffer.indexOf("\n\n"))>=0) {
        const frame=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);
        const data=frame.split("\n").filter(line=>line.startsWith("data:")).map(line=>line.slice(5).trimStart()).join("\n");
        if(data) receive(JSON.parse(data));
      }
      if(buffer.length>2_000_000) throw new Error("Builder event exceeded its size limit.");
      if(done) break;
    }
  } finally {await reader.cancel();reader.releaseLock();}
}

/** Require a completed run before making a streamed proposal reviewable. */
export async function runBuilder(options, {signal,activity,token=""}={}) {
  const {prompt,...forwardedProps}=options;
  const response=await fetch("/api/agui",{method:"POST",signal,
    headers:{"Content-Type":"application/json",...(token?{Authorization:"Bearer "+token}:{})},
    body:JSON.stringify({threadId:crypto.randomUUID(),runId:crypto.randomUUID(),
      messages:[{id:crypto.randomUUID(),role:"user",content:prompt}],state:{},tools:[],context:[],forwardedProps})});
  let proposal,finished=false;
  await readEvents(response,event=>{
    if(event.type==="RUN_ERROR") throw new Error(event.message);
    if(event.type==="RUN_FINISHED") finished=true;
    if(event.type==="CUSTOM" && event.name==="studio.activity") activity?.(event.value.message);
    if(event.type==="CUSTOM" && event.name==="studio.proposal") proposal=event.value;
  });
  if(!finished || !proposal) throw new Error("The builder disconnected before completing its proposal. Please retry.");
  return proposal;
}

/** Keep visible progress bounded and retained in the project's conversation. */
export function activityCard(host, cancel) {
  const title=document.createElement("strong"),elapsed=document.createElement("span"),list=document.createElement("ol"),stop=document.createElement("button");
  title.textContent="Builder is working";elapsed.className="builder-elapsed";elapsed.setAttribute("aria-hidden","true");
  stop.type="button";stop.className="button";stop.textContent="Stop";stop.addEventListener("click",cancel);
  host.replaceChildren(title,elapsed,list,stop);host.setAttribute("role","status");host.setAttribute("aria-live","polite");
  const start=Date.now(),timer=setInterval(()=>{elapsed.textContent=`${Math.floor((Date.now()-start)/1000)}s elapsed`;},1000);
  let last="";
  return {
    update(text) {
      if(text===last)return;
      const parent=host.parentElement,following=parent.scrollHeight-parent.scrollTop-parent.clientHeight<80;
      last=text;const item=document.createElement("li");item.textContent=text;list.append(item);
      if(list.children.length>12)list.firstChild.remove();
      // Follow incoming work only while the user is already at the conversation end.
      if(following)stop.scrollIntoView({block:"nearest"});
    },
    finish(text) {clearInterval(timer);title.textContent=text;stop.remove();host.classList.remove("pending");},
  };
}
