# Repository guidance

Follow [docs/development.md](docs/development.md) for Harnest source-quality and
verification requirements.

## Codex implementation guidance

- When building or modifying the Harnest compiler, runtime, CLI, or engine, add
  a concise docstring or function comment to every new or materially modified
  non-trivial function or method.
- Add inline comments that explain the reason for non-obvious decision logic,
  ownership boundaries, ordering, safety, and compatibility behavior. Do not
  narrate syntax or restate what the code already makes clear.

## Documentation ownership

- Canonical public Harnest documentation lives in the `Usefused/mintlify-docs`
  repository under `harnest/` and is authored as `.mdx`.
- Link readers to `https://docs.usefused.com/harnest` and its deployed child
  routes. Do not use GitHub source-file URLs as public documentation links.
- Make every public product-documentation update in that directory and keep its
  Harnest tab in `docs.json` current. Do not add new end-user guides under this
  repository's `docs/` directory.
- Keep this repository's `README.md` limited to what Harnest is, installation,
  initialization, migration or framework switching, serving, and a link to the
  canonical documentation.
- Treat existing files under `docs/` as repository engineering references.
  Update them only when a source-quality or implementation contract explicitly
  depends on the local reference. If the Mintlify checkout is unavailable, ask
  for its location instead of creating a second public guide here.

## Skills

- Keep every `SKILL.md` at 400 words or fewer, including YAML frontmatter.
- Write skill entrypoints around outcomes and actions. State the result to
  produce and the decisions the agent must make.
- Move conditional detail, schemas, background, and extended examples into
  linked `references/` files.
