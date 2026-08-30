# External Hatchet live fixture

This fixture follows Hatchet's supported Lite + PostgreSQL Docker topology. It
also runs a separate PostgreSQL 16 database for Harnest sessions, checkpoints,
and continuations. Hatchet never reads or migrates that database, and the
Hatchet engine and worker remain running when an agent server stops.

1. Start Hatchet Lite and create a local API token at
   `http://localhost:8888` (default login: `admin@example.com` / `Admin123!!`).
   Export it without writing it into the repository:

   ```bash
   export HATCHET_CLIENT_TOKEN='<local development token>'
   ```

2. Configure the host SDK and start the complete external stack:

   ```bash
   export HATCHET_CLIENT_HOST_PORT='localhost:7077'
   export HATCHET_CLIENT_TLS_STRATEGY='none'
   export HATCHET_CLIENT_SERVER_URL='http://localhost:8888'
   export HARNEST_HATCHET_POSTGRES_URL='postgresql://harnest:harnest-local-only@127.0.0.1:55447/harnest'
   docker compose -f tests/fixtures/hatchet/compose.yaml --profile live up -d --build
   ```

   `harnest-local-only` is a deterministic credential for this loopback-bound
   fixture, not a production secret. Override both sides without committing the
   value when needed:

   ```bash
   export HARNEST_POSTGRES_PASSWORD='<local password>'
   export HARNEST_HATCHET_POSTGRES_URL="postgresql://harnest:${HARNEST_POSTGRES_PASSWORD}@127.0.0.1:55447/harnest"
   ```

The Harnest database initializes only the `harnest_runtime` namespace and makes
it the role's default schema. `PostgresStore` remains responsible for creating
and migrating its own tables. Keeping this boundary in the runtime avoids a
second, fixture-owned copy of Harnest's storage schema.

The worker accepts `consumer-report` jobs. It records only a Harnest correlation
identifier and Hatchet run ID. `GET http://localhost:8099/evidence/<id>` proves
the job started; `POST http://localhost:8099/release/<id>` lets it complete.

The RuntimePlugin source is `examples/plugins/hatchet`. Copy it to the
ADK consumer agent as `plugins/hatchet` and add `hatchet-sdk>=1.38,<2` to the
root agent `pyproject.toml`. The plugin has no `tools/` directory by design.

After installing the SDK, exercise the provider-only live boundary with:

```bash
HARNEST_HATCHET_LIVE=1 .venv/bin/python -m pytest -q \
  tests/python/test_hatchet_plugin_docker_live.py
```

Exercise the complete ADK agent → consumer-owned tool → Hatchet → native
model-loop resume journey with the repository's deterministic test model:

```bash
HARNEST_HATCHET_LIVE=1 .venv/bin/python -m pytest -q \
  tests/python/test_hatchet_plugin_consumer.py::HatchetConsumerDockerLiveTests
```

The `hatchet_consumer_real` overlay runs the ADK consumer on the host. It reads
`HARNEST_HATCHET_POSTGRES_URL`, requires an explicit `LITELLM_MODEL`, and accepts
an optional `LITELLM_API_BASE`; the selected provider's credential also stays in
the host environment. Model credentials must not be passed to this Compose
stack: Hatchet owns job execution, while the Harnest agent owns model execution.
Compose intentionally does not modify agent storage or model configuration.

Run the complete real-provider and cross-replica durable-restart journey with:

```bash
export LITELLM_MODEL='openai/gpt-4.1-mini'
export HARNEST_HATCHET_REAL_LIVE=1
.venv/bin/python -m pytest -q \
  tests/python/test_hatchet_plugin_consumer.py::HatchetConsumerRealModelPostgresLiveTests
```

Provide the credential expected by the selected LiteLLM provider only in the
host environment. The test records model-boundary phases but never prompts,
responses, credentials, or provider job identifiers.
