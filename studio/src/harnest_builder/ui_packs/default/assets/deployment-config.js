import {el,button,field,select} from "./ui.js";
import {activeField} from "./components.js";

/** Turn discovered dependencies into a reviewable manifest without applying infrastructure. */
export function configureDeployment({inspection,project,api,isDirty,reviewProposal,notice,modal}) {
  let name,image,backend,context,namespace,port,memory,cpus,variables,services,network;
  const form=modal("Configure deployment","Studio detects runtime inputs from your agent source. Choose what to run and what to connect to, then review the generated files.","Generate configuration",async()=>{
    if(isDirty())throw new Error("Save your source edits before generating deployment configuration.");
    const proposal=await api("deployment/propose","POST",{project,name:name.value,image:image.value,backend:backend.value,context:context.value,namespace:namespace.value,port:Number(port.value),memory:memory.value,cpus:Number(cpus.value),variables:variables.value.split(/[\s,]+/).filter(Boolean),services_yaml:services.value,network_yaml:network.value});
    reviewProposal(project,proposal);
    notice("Deployment files generated. Review and apply the source changes before previewing deployment.");
  },true);
  name=field(form,"Deployment name",inspection.name,{required:true});
  backend=select(form,"Target",[["local","Local · Docker Compose"],["kubernetes","Kubernetes"]],"local");
  image=field(form,"Agent container image",inspection.name+":local",{required:true,hint:"Build this image with the compiled agent, dependencies and any stdio MCP executables. Its entrypoint must start the server on 0.0.0.0 and the port below."});
  context=field(form,"Kubernetes context","",{placeholder:"Your kubeconfig context"});
  namespace=field(form,"Existing namespace","",{placeholder:"Your Kubernetes namespace"});
  port=field(form,"Agent HTTP port","1907",{type:"number",min:1024,max:65535,required:true});
  memory=field(form,"Agent memory",inspection.resources.memory||"512Mi",{required:true,placeholder:"512Mi or 1Gi"});
  cpus=field(form,"Agent CPUs",inspection.resources.cpu||"1",{type:"number",min:0.01,max:128,step:0.01,required:true});
  variables=field(form,"Runtime environment variables",inspection.variables.join("\n"),{multiline:true,rows:5,hint:"One name per line. Values are supplied by the Studio/CLI environment; no credentials are copied into YAML."});
  services=field(form,"Services",inspection.services_yaml,{multiline:true,rows:12,hint:"mode: connect keeps an external endpoint; mode: provision runs an image. provides injects settings into the agent. Delete optional services you do not want."});
  form.append(button("Add Redis service example",()=>{const current=services.value.trim();if(/^  ?cache:/m.test(current)||/^cache:/m.test(current))throw new Error("A cache service is already defined.");services.value=(current==="{}"?"":current+"\n")+"cache:\n  mode: provision\n  type: redis\n  image: redis:7.4\n  ports: {redis: 6379}\n  healthcheck: {command: [redis-cli, ping]}\n  persistence: {mount: /data, size: 5Gi}\n  provides:\n    REDIS_URL: redis://${services.cache.host}:${services.cache.ports.redis}/0\n";}));
  network=field(form,"Agent network settings","{}",{multiline:true,rows:3,hint:"Optional YAML: hosts: {host.docker.internal: host-gateway} for local host access, or hostname-to-IP mappings. dns: [IP] replaces default DNS; omit it to retain service discovery."});
  for(const note of inspection.warnings)form.append(el("p",note,"empty-note"));
  const update=()=>{const kube=backend.value==="kubernetes";activeField(context,kube,true);activeField(namespace,kube,true);};
  backend.addEventListener("change",update);update();
}
