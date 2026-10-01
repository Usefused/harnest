import {el, field, select} from "./ui.js";

/** Disabled hidden controls cannot veto another mode's native form validation. */
export function activeField(control, active, required=false) {
  control.parentElement.hidden=!active;
  control.disabled=!active;
  control.required=active&&required;
}

/** Use native controls and serialize only the fields relevant to the chosen mode. */
export function cronFields(form, files) {
  const tasks=files.filter(p=>/^tasks\/[^_][^/]*\.py$/.test(p)).map(p=>[p.slice(6,-3),p.slice(6,-3)]);
  const modes=[["fixed","Fixed schedule · new cron function"],["dynamic","Dynamic target · schedule from code"]];
  if(tasks.length)modes.push(["existing","Schedule an existing task"]);
  const mode=select(form,"Schedule mode",modes,"fixed");
  const schedule=field(form,"UTC schedule","0 9 * * *",{hint:"Five fields: minute hour day month weekday. Runs at 09:00 UTC by default."});
  const task=select(form,"Existing task",tasks);
  const args=field(form,"Arguments (JSON object)",'{"payload": "scheduled"}',{multiline:true,rows:3,hint:"Match the task parameters. Never include credentials or private input."});
  const queue=field(form,"Queue","default",{required:true});
  const retries=field(form,"Maximum retries","3",{type:"number",min:0,max:100,required:true});
  const note=el("p","","empty-note");form.append(note);
  const update=()=>{
    const dynamic=mode.value==="dynamic", existing=mode.value==="existing";
    activeField(schedule,!dynamic,true);activeField(args,!dynamic,true);
    activeField(task,existing,true);
    activeField(queue,!existing,true);activeField(retries,!existing,true);
    note.textContent=dynamic?"Creates @cron() without a schedule. Use harnest.cron.create(key=..., expression=..., task=..., arguments=...) from runtime code. Configure task and cron storage before serving.":"Configure task and cron storage before serving. Existing tasks retain their own queue and retry policy. Compile to validate arguments against the target task.";
  };
  mode.addEventListener("change",update);update();
  return ()=>({mode:mode.value,...(mode.value==="dynamic"?{}:{schedule:schedule.value,arguments:args.value}),...(mode.value==="existing"?{task:task.value}:{queue:queue.value,max_retries:retries.value})});
}

/** Keep persistence and private-input prerequisites visible before source is created. */
export function runtimeFields(kind, form, files) {
  if(kind==="cron")return cronFields(form,files);
  if(kind==="task-storage") {
    const provider=select(form,"Provider",[["memory","Memory · local development only"],["postgres","Postgres · durable tasks and schedules"],["redis","Redis · requires configured persistence"]]);
    form.append(el("p","Each storage role must have one provider. Replace existing task/cron declarations before compiling. Managed environments include both drivers. Sync the environment, then supply DATABASE_URL for Postgres or REDIS_URL for Redis. Direct Python installations need the matching Harnest extra.","empty-note"));
    return ()=>({provider:provider.value});
  }
  if(kind==="client-input")form.append(el("p","Define the private schema and implement its async handler before serving. The first handler parameter is private; only the fixed response is sent to the model. Do not log or store the submitted input. Pending input requires the same server process.","empty-note"));
  return ()=>({});
}

/** Display backend prerequisites alongside CLI-owned metric IDs, including custom scorers. */
export function evaluationFields(form, metrics) {
  const metric=select(form,"Evaluation metric",metrics.map(item=>[item.id,`${item.label} · ${item.backend}`]),"response_match_score");
  form.append(el("p","Works with ADK and LangGraph. Criteria apply to all evaluation sets and existing settings are preserved. Judge, simulator, and Vertex presets need provider configuration. Custom creates a Python scorer stub to implement and register in test_config.json. Live evaluations call the agent's model and tools.","empty-note"));
  return metric;
}

/** Expose validated CLI policies while excluding unrelated controls from requests. */
export function executionFields(form, action) {
  const input=field(form,"Prompt for the agent","",{multiline:true,rows:3,placeholder:"What can you help me with?"});
  const port=field(form,"Local server port","1907",{type:"number",min:1024,max:65535});
  const extension=field(form,"Extension package or slug","docker",{placeholder:"docker, rag, hatchet, or a package name"});
  const trajectory=select(form,"Tool trajectory matching",[["business","Business · required calls in order"],["strict","Strict · exact authored call sequence"]],"business");
  const profile=select(form,"Dependency profile",[["runtime","Runtime · serving dependencies"],["compile","Compile · artifact creation"],["development","Development · local testing"],["eval","Evaluation · scoring dependencies"]],"runtime");
  const hint=el("p","","empty-note");form.append(hint);
  const update=()=>{
    activeField(input,action.value==="run",true);activeField(port,action.value==="serve",true);
    activeField(extension,action.value==="install-extension",true);
    activeField(trajectory,action.value==="eval");activeField(profile,action.value==="sync");
    const notes={serve:`The server will listen at http://127.0.0.1:${port.value}. Watch command output for readiness.`,run:"Enable spec.interfaces.cli: true in config.yaml. Live calls use the agent's configured model and tools; results appear in the terminal.",eval:"Evaluations call the agent's configured models and tools. Business matching allows extra calls; strict matching requires the exact authored sequence.",sync:"Sync updates the selected profile's lock and environment. Commit the lock files used by your project and CI."};
    hint.textContent=notes[action.value]||"Commands use the project on disk. Results and failures appear in the terminal.";
  };
  form.addEventListener("change",update);update();
  return ()=>{
    const values={run:{input:input.value},serve:{port:Number(port.value)},"install-extension":{name:extension.value},eval:{eval_trajectory:trajectory.value},sync:{environment_profile:profile.value}};
    return values[action.value]||{};
  };
}
