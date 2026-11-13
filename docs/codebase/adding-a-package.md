# Shipping your own code in a Harnest build

There are three ways to add your own Python code to a Harnest fork. Pick by
how agents should receive it.

| Way | How agents get it | Use it when |
| --- | --- | --- |
| [In-tree module](#1-an-in-tree-module) in `src/` | Automatically: it is inside the wheel your CLI embeds | Everyone using your build needs it: an internal auth integration, a storage backend, a transport |
| [Companion package](#2-a-companion-package) in `packages/` | When an agent lists it in its `pyproject.toml` | It is optional and should be versioned and released on its own |
| [Harnest Extension](#3-a-harnest-extension) | When an agent installs it with `harnest extensions install` | It plugs into an agent's lifecycle: storage, sandboxes, channels, hooks |

To change the CLI itself (new subcommands, new scaffolds), see
[README.md](README.md#common-changes). To give every new agent your defaults
and a company command, see [packs.md](packs.md).

## How Python reaches an agent

The `harnest` CLI does not install Harnest from PyPI. Release builds embed one
Harnest wheel ([internal/runtimewheel](../../internal/runtimewheel/wheel.go)),
built from this repository by `scripts/build_runtime_wheel.py`. Each agent gets
its own environment that installs that wheel with the extras its command needs
(`environmentProfile.wheelExtras` in
[environment_profile.go](../../cmd/harnest/environment_profile.go)), plus the
agent's own dependencies from its `pyproject.toml`. Everything is pinned in a
hashed lock and installed with uv.

So code inside the wheel is always present, while anything else must be named
by the agent.

## 1. An in-tree module

1. **Add the file.** Put it in `src/harnest/<name>.py` (or a subpackage). Start
   it with a module docstring whose first sentence says what it owns; that
   sentence becomes its line in [files.md](files.md). Prefix the name with `_`
   if agents must not import it. If no area in `scripts/codebase_map.py`
   `AREAS` fits the name, add a pattern there. Then run
   `python scripts/codebase_map.py`.

   A new top-level package (like `src/harnest_postgres`) also needs adding to
   `packages` under `[tool.hatch.build.targets.wheel]` in `pyproject.toml`, or
   it is left out of the wheel.

2. **Keep third-party imports lazy.** Importing `harnest` must stay cheap and
   must not need optional packages. Import optional libraries inside the
   function that uses them, and raise a clear error naming the missing package.

3. **Declare its dependencies.**
   - Needed by every agent: add them to `[project] dependencies`.
   - Needed only by agents that use your module: add an extra under
     `[project.optional-dependencies]`. Then teach the CLI when to install it.
     Add the condition to `runtimeDependencyPlan`
     ([runtime_dependency_plan.go](../../cmd/harnest/runtime_dependency_plan.go))
     and append the extra in `wheelExtras`, the same way `HasMCP` selects the
     framework's MCP extra.
   - Framework packages never go here. They live in `frameworks.json` (see
     [../frameworks.md](../frameworks.md)).

4. **Expose it, if authors import it.** Public names are a contract. List them
   in the module's `__all__`, and add the module to the public domains in
   `src/harnest/__init__.py` if it is a new top-level domain. Then record them
   in `tests/fixtures/public-api.json`. The typing gate (`make typing-quality`)
   requires full annotations on everything in the package.

5. **Test it.** Add `tests/python/test_<name>.py` and register it in
   `tests/python/suites.json`: use `unit` for pure Python, `integration` when
   it needs processes, databases, or a framework.

6. **Build your CLI.**
   - **During development:** `go build -o bin/harnest ./cmd/harnest`, then
     point the CLI at a Python with your checkout installed in editable mode:
     `HARNEST_PYTHON=$PWD/.venv/bin/python bin/harnest serve my-agent`. Source
     builds have no embedded wheel, so they use that interpreter directly.
   - **For release:** `.goreleaser.yaml` builds the wheel, uv, and agent
     launchers, then compiles the CLI with the `harnest_release` tag so it
     embeds them. Run it with your own version and distribution settings.

## 2. A companion package

`packages/harnest-fused` is the model: a separate distribution that depends on
`harnest` and uses only its public API. How to use it is in the public
[Compose APIs with Fused](https://usefused.com/docs/harnest/build/fused-mcp)
guide and the
[`harnest_fused` reference](https://usefused.com/docs/harnest/reference/api/harnest_fused).

To add another package to this repository, give it its own
`packages/<name>/pyproject.toml` with a `harnest` range that includes the
version your CLI embeds. Then add `packages/<name>/src` to `pythonpath` under
`[tool.pytest.ini_options]` in the root `pyproject.toml` and to the paths in
`scripts/run_python_tests.py`, and register its tests in
`tests/python/suites.json`. Agents use it by listing it in their own
`pyproject.toml`. Templates supply default dependencies before
[pack initialization](packs.md#dependency-ownership).

## 3. A Harnest Extension

Extensions are covered by the public docs:
[Create a Harnest Extension](https://usefused.com/docs/harnest/build/extensions/create),
[Use a Harnest Extension](https://usefused.com/docs/harnest/build/extensions/use),
and [Extension lifecycle](https://usefused.com/docs/harnest/build/extensions/lifecycle).
The repository-side contract is in [../extensions.md](../extensions.md), and
`official-extensions/` holds working examples.
