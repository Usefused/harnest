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

## Skills

- Keep every `SKILL.md` at 400 words or fewer, including YAML frontmatter.
- Write skill entrypoints around outcomes and actions. State the result to
  produce and the decisions the agent must make.
- Move conditional detail, schemas, background, and extended examples into
  linked `references/` files.
