# Runtime metadata demo

This deterministic managed Graph demonstrates `Graph(output_schema=...)` and
an explicit `FrameworkMetadata[T]` field. It needs no model server or API key.

From the repository root, run the same authored application through both
framework adapters:

```bash
PYTHONPATH=src .venv/bin/python -m harnest.cli test \
  examples/runtime-metadata --smoke --framework adk

PYTHONPATH=src .venv/bin/python -m harnest.cli test \
  examples/runtime-metadata --smoke --framework langgraph
```

Each smoke run calls the neutral `/responses` API and checks that:

- the Graph's terminal value satisfies `MetadataResult`;
- the provider-independent answer is unchanged;
- exactly one of `metadata.adk` or `metadata.langgraph` is populated; and
- the active namespace retains native events or messages for that turn; and
- `GET /sessions/{id}/messages` exposes an ordered portable transcript while
  retaining the active native record under each message's `metadata`.

The `metadata` field is absent from the provider-owned portion of the output
contract and is filled by Harnest only after framework execution completes.
