# Code quality

Apply these rules when changing Harnest's own Python and Go source. An authored
agent repository follows its own project standards unless the user says
otherwise.

- Keep every function and method at cyclomatic complexity 10 or below. Extract
  cohesive helpers; do not suppress or game the check.
- Keep shared behavior in one place and separate policy from I/O, framework,
  transport, serialization, and persistence concerns.
- Explain why non-obvious policy, ordering, or safety decisions exist. Avoid
  comments that merely translate code into prose.
- Push filtering, ordering, projection, and pagination into query-backed stores.
  Batch or join related reads instead of issuing one query per item, and add a
  query-count test when persistence behavior changes.
- For durable runtime changes caused by a user or agent, emit privacy-safe OTEL
  audit telemetry after commit and on failure. Do not include prompts, payloads,
  results, credentials, or secrets. Harnest compiler and CLI filesystem changes
  are currently exempt from this OTEL requirement.
- Add focused unit tests, integration tests for crossed boundaries, and an E2E
  test only when a released journey cannot be covered reliably at a smaller
  boundary.

Install `.[all,quality]` and run `make quality` before completing a Harnest
source change.

For release changes, preserve the single-binary invariant: GoReleaser builds
the version-matched Python wheel and verified platform-native `uv` into the Go
executable before compilation, release archives contain no sidecar wheel or
bootstrapper, and `harnest runtime install` is the only supported bootstrap path
for that embedded runtime.
