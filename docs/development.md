# Harnest development standards

These rules apply to the Go engine/CLI and the Python compiler/runtime. They are
release requirements, not conventions that may be bypassed when a change is
small.

## Design and implementation

- Keep cyclomatic complexity at 10 or below for every function and method.
  Extract policy, validation, I/O, and formatting into cohesive helpers instead
  of hiding branches or suppressing the check.
- Keep one source of truth for shared behavior. Prefer a small shared helper or
  boundary type over copied framework, command, transport, or validation logic.
- Separate domain decisions from filesystem, process, network, database, and
  serialization concerns so each can be tested without unrelated infrastructure.
- Comment the reason for non-obvious policy, ordering, safety, or compatibility
  decisions. Do not narrate syntax or restate the code.
- Query-backed code must apply predicates, projection, ordering, and pagination
  in the datastore. Do not load a result set and then filter it in Go, and do not
  issue one query per item when a batch, join, prefetch, or set-based operation
  can retrieve the same data. Add query-count tests around such paths.

The datastore rule does not prohibit bounded filesystem discovery or decoding a
single validated document: those sources do not offer a query planner. Keep such
work streaming or bounded where practical and avoid repeated reads.

## OpenTelemetry audit boundary

Runtime operations that change durable state because of a user- or agent-
triggered execution must emit a privacy-safe OTEL audit signal after the change
is committed, with a correlated failure signal when an attempted change fails.
Record the operation, trigger, outcome, and stable low-cardinality identifiers;
never record prompts, results, credentials, headers, secret values, or complete
payloads.

Compiler and CLI filesystem changes are intentionally outside this OTEL audit
boundary for now. Do not add a second telemetry stack to `harnest compile`,
`harnest init`, mode checks, or skill installation until compiler telemetry is
designed explicitly.

## Tests and quality gates

Every behavior change needs focused unit tests and an integration test when the
change crosses a compiler/backend, process, transport, datastore, or framework
boundary. Add an end-to-end test only when the behavior cannot be established at
a smaller boundary or when a released user journey is at risk.

Run the complete local gate before submitting a change:

```bash
python -m pip install -e ".[all,quality]"
make quality
```

`make quality` validates schemas, runs Python and Go tests, checks both languages
for complexity above 10, verifies Go formatting, runs `go vet`, and exercises the
offline Python-to-Go plan/compile/deploy contract. The same gate runs for pull
requests and before a release is published from `main`.
