# Deployment authoring contract

Inspect `config.yaml`, agent and lifecycle/storage source, MCP definitions,
extensions, and dependency files. Do not execute source to discover inputs.
Literal environment reads can be discovered statically; computed names and
dynamic dependencies require review. A PostgreSQL fallback is an option, not
evidence that a database is already running. Subagents and stdio MCP clients
normally execute inside the agent image; include their executable dependencies.
When changing storage, model endpoints, MCP, or external services, read the
existing deployment file too. Keep bindings and `depends_on` consistent. Switching
to `MemoryStore` removes unused database services and `DATABASE_URL` bindings;
preserve a database if another component still needs it.

`harnest-deployment.yaml` is a **single Harnest deployment document**. It is not a
Kubernetes manifest. Raw Kubernetes Deployment/Service streams, including `---`
separators, belong in a separate file such as `deploy/kubernetes.yaml`.

CPU and memory limits belong under each workload's `resources` in this deployment
file. Do not generate `spec.resources.cpu` or `spec.resources.memory` in the
agent's `config.yaml`; those fields remain accepted only for older projects.

Use this shape, adapting names, images and bindings to known user inputs:

```yaml
version: 1
name: support
backend: local
services:
  database:
    mode: connect
    type: postgres
    url: {secret: DATABASE_URL}
    variable: DATABASE_URL
  cache:
    mode: provision
    type: redis
    image: redis:7.4
    ports: {redis: 6379}
    command: [redis-server, --appendonly, 'yes']
    healthcheck: {command: [redis-cli, ping]}
    persistence: {mount: /data, size: 5Gi}
    provides:
      REDIS_URL: redis://${services.cache.host}:${services.cache.ports.redis}/0
agents:
  support:
    image: registry.example/support:release
    ports: {http: 1907}
    publish: {http: 1907}
    depends_on: [database, cache]
    environment:
      OPENAI_MODEL: your-model
      OPENAI_API_KEY: {secret: OPENAI_API_KEY}
      OPENAI_BASE_URL: {secret: OPENAI_BASE_URL}
    resources: {cpus: 1.0, memory: 1Gi}
    healthcheck:
      command: [python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:1907/health', timeout=2)"]
```

Replace example image/model values with user inputs; never claim an image was
built or pushed. The image must contain a compiled agent, runtime dependencies,
and an entrypoint that starts its HTTP server on `0.0.0.0` at the declared port.
`command` replaces image CMD (Kubernetes args), not its entrypoint. Authentication
remains the agent's lifecycle responsibility; do not disable it for deployment.
Images may need Node/npx or other tools for stdio MCP.

`mode: connect` injects existing external endpoints and does not create or remove
infrastructure. `mode: provision` requires an image and exec healthcheck, and may
declare ports, environment, resource limits, persistence and dependencies.
Secret references resolve from the process launching Studio or the CLI only at
apply time. Do not write secrets into source or export redacted strings as real
credentials. A dependency's `provides` variables cannot conflict with another
dependency or the agent's `environment`. Host/port placeholders reference only
provisioned components. Configure the agent to consume those environment names;
setting an unused variable does not change its storage or model implementation.

For Kubernetes set `backend: kubernetes`, an explicit kubeconfig `context`, and
an existing `namespace`. Never guess the cluster or namespace. Remove local
`publish` ports; the generated Service is internal. Replicas greater than one
cannot use fixed published ports or single-writer persistence.

Containers share service DNS by default. Use `${services.NAME.host}` and
`${services.NAME.ports.PORT}` for provisioned endpoints. Localhost refers to the
container itself, not the developer machine. Optional per-container settings:

```yaml
network:
  hosts: {internal-api.example: 10.0.0.12}
  dns: [10.0.0.53]
```

Host aliases require IP addresses; local Compose additionally permits
`host-gateway`. Kubernetes rejects that special value. Custom DNS replaces
default service discovery: omit it unless the supplied resolver can resolve the
required internal names. These settings do not grant host networking, expose
public ingress, or configure VPNs. Those require explicit infrastructure work.

Preview with `harnest provision plan --project AGENT_DIR`. Apply only when the
user requests deployment and image, secrets and target prerequisites are ready.
The provisioner renders Compose or Kubernetes from this same declaration,
records revisions, and retains persistent volumes on removal. Workload-only
Kubernetes previews omit Secrets; use the provisioner or create all required
Secrets before applying those previews directly.
