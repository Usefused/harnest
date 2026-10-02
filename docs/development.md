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
- Expose authoring contracts through public domain modules such as `harnest.auth`,
  `harnest.agent`, and `harnest.context`. Keep implementation helper-module paths
  out of public examples. Re-export the original objects so existing exception
  handlers, decorators, and type checks remain compatible when imports change.
- Treat every name in `__all__` for a snapshotted public Python module as a
  compatibility commitment. Removing or renaming an export requires an explicit
  breaking-release decision; adding one requires reviewing and updating
  `tests/fixtures/public-api.json`. Preserve object identity when moving a public
  contract between modules.
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

## Playground controls

Use native `select` and `option` elements as the value and validation contract.
The shared `selects.js` enhancer applies the standard dropdown to existing and
newly rendered single selects automatically; do not build page-specific option
menus. `selects.css` owns trigger, menu, selected, disabled, and focus styling.
Specialized pickers that retain search or extra metadata must reuse these style
primitives and explicitly associate their native backing select with its trigger
using `data-select-proxy`. Verify keyboard selection and narrow-screen placement.

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

Keep every repository `SKILL.md` at 400 words or fewer, including frontmatter.
Write skill entrypoints around outcomes and actions: tell the coding or runtime
agent what result to produce and what decisions to make. Move conditional API
detail, schemas, examples, and background into linked `references/` files. The
`skill-quality` gate enforces the word ceiling; reviewers enforce usefulness and
action orientation.

Every behavior change needs focused coverage at the smallest boundary that can
establish it. Use an integration test for compiler/backend, process, transport,
datastore, or framework contracts. Add an end-to-end test only when the behavior
cannot be established at a smaller boundary or when a released user journey is
at risk.

Extend an existing test when it already exercises the changed contract. Add a
separate test for a distinct failure mode or behavior, not just another helper
or layer on the same path. Prefer observable results over assertions about
private wrapper shapes or mocked delegation. When removing overlapping tests,
identify the retained test and preserve any unique assertions. Shared fixtures
and tables should reduce maintenance; fewer test methods alone is not a goal.

Every Python test module is assigned exactly one default tier in
`tests/python/suites.json`. A narrow override may assign an individual class or
method to a broader tier when a module contains both local and external-provider
coverage. The suite runner rejects missing files, unclassified modules, duplicate
assignments, and stale overrides.

Public namespace changes must preserve IDE-visible types as well as runtime
imports. The public-import tests run Pyright against `tests/typing/` consumer
fixtures, checking inferred types and rejection of invalid calls. Keep lazy
exports statically discoverable without eagerly loading optional frameworks or
caching invocation-scoped values. Generated consumer probes cover every reviewed
public export, inherited Harnest methods, properties, fields, and Python protocol
methods. Decorator fixtures verify original argument and return types, including
invalid calls. The `typing-quality` gate also requires parameter and return
annotations on every core function and method, including private, nested, and
optional-backend implementations. Its source inventory follows the wheel
exclusions in `pyproject.toml`, keeping retired local prototypes out of the count.
Use concrete contracts for known capabilities;
reserve `Any` for genuinely dynamic payloads and untyped external boundaries.
Pyright is included in the `quality` extra.

- `make test-unit` runs isolated domain behavior without infrastructure boundaries.
- `make test-integration` runs compiler, framework, process, transport, and local
  datastore boundaries.
- `make test-e2e` runs a small set of released authoring and serving journeys.
- `make test-live` runs credential- or service-dependent provider checks; unavailable
  services remain explicit skips.
- `make test` runs every Python tier plus all Go tests.

Run the complete local gate before submitting a change:

```bash
python -m pip install -e ".[all,quality]"
make quality
```

`make quality` validates schemas, runs Python and Go tests, checks both languages
for complexity above 10, verifies Go formatting, runs `go vet`, and exercises the
offline Python-to-Go plan/compile/deploy contract. The same gate runs for pull
requests and before a release is published from `main`.

## Internal orchestration validation

`make dry-run` runs `internal/devtools/orchestrator` to exercise the Python-to-Go
compile/deploy contract. This driver is repository development tooling and is
not a released command. Keep its deployment opt-in and engine validation in
the quality gate; `cmd/` contains the public CLI and the embedded agent launcher.

## Studio editor assets

Studio ships the local CodeMirror bundle in
`studio/src/harnest_builder/static/editor.js`; browsers never fetch editor
code from a CDN. After changing `studio/frontend/`, run `npm ci` and
`npm run build` there and commit the regenerated bundle and license notices.
Ruff runs in the Studio Python environment on unsaved stdin with isolated
correctness rules; diagnostics never execute source or apply fixes.

## Studio Pack implementation contract

Studio Pack manifests and source snapshots are parsed without importing pack
code. The UI and builder consume the same catalog. Pack reviews retain exact
source revisions and installation receipts; upgrades must not overwrite locally
modified installed files. Company wheels include only manifest-referenced pack
files and depend on an exact Studio package version. They do not vendor Python,
the Harnest CLI, credentials, or third-party runtime dependencies.

Builder defaults are app-scoped: loaded packs overlay fields in launch order,
then explicit `HARNEST_BUILDER_*` environment settings override them. Request
model/budget selections override those defaults. Pack CA files are bounded,
unlinked PEM certificate snapshots included in wheel integrity records; private
keys are rejected. Resolve credential environment references only at launch.
Never mutate process-wide environment or place provider settings in model
context. Test TLS through the compiled assistant, not only a mocked completion.
