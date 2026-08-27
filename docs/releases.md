# Installation and releases

## Install the standalone CLI

Harnest releases contain a native `harnest` executable and the matching Python
wheel. The installer supports macOS and Linux on AMD64 and ARM64. It requires
Python 3.10 or newer only to create the managed environment; normal Harnest
commands then use that environment automatically.

Install the latest release from the canonical repository:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/creativeJoe007/harnest/main/install.sh |
  sh
```

For a pinned version or a fork, set variables on the `sh` process receiving the
script:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/creativeJoe007/harnest/main/install.sh |
  HARNEST_VERSION=v0.1.0 HARNEST_REPO=creativeJoe007/harnest sh
```

`HARNEST_VERSION` accepts a release version with or without the leading `v`.
When omitted, the installer follows the repository's latest GitHub Release.
`HARNEST_REPO` defaults to `creativeJoe007/harnest` and accepts an
`owner/repository` override. Set `GITHUB_TOKEN` for a private fork; the installer
does not print it.

The installer downloads the platform archive and `checksums.txt` from the same
GitHub Release, requires a matching SHA-256 entry, verifies the archive before
extracting it, and rejects a bundled wheel whose version differs from the
release. It then:

- creates or updates a dedicated virtual environment at
  `${HARNEST_RUNTIME_DIR:-$HOME/.harnest/runtime}`;
- installs the bundled wheel and its Python dependencies into that environment;
  and
- atomically installs the native executable at
  `${HARNEST_INSTALL_DIR:-$HOME/.local/bin}/harnest`.

`HARNEST_BOOTSTRAP_PYTHON` selects the Python used to create the environment.
The selected interpreter must be Python 3.10 or newer. `HARNEST_PYTHON` is
different: it overrides the interpreter used later by the `harnest` command.
The CLI normally resolves Python in this order: `--python`, `HARNEST_PYTHON`,
the managed `HARNEST_RUNTIME_DIR`, then `python3` on `PATH`.

Add the install directory to `PATH` if necessary, then verify both layers:

```bash
harnest --version
harnest doctor
```

Rerunning the installer upgrades the managed wheel before atomically replacing
the executable. For a review-first installation, download `install.sh`, inspect
it, then execute the local file with the same environment variables.

Harnest releases also define the supported ADK and LangGraph version ranges.
Those bounds are published in `pyproject.toml`, installed with the matching
wheel, and copied into newly scaffolded agent requirements. Compilation checks
the installed selected framework against the release's bounds in both managed
and advanced mode. To use a newer framework outside them, install a newer
Harnest release that declares support before changing the agent dependency.
Direct ADK or LangGraph imports in advanced mode do not bypass this check.

## Publish a release

Releases use GoReleaser v2.18 or newer. Each platform archive contains:

- the native `harnest` executable built from `cmd/harnest`;
- the matching universal Python wheel under `python/`;
- `README.md`; and
- the Apache 2.0 `LICENSE`.

GoReleaser also publishes `checksums.txt` with SHA-256 checksums for every
archive. Releases are explicitly targeted at `creativeJoe007/harnest`, matching
the installer's default source.

Before tagging, ensure the static version in `pyproject.toml` exactly matches
the tag without its leading `v`, and keep the source-tree fallback in
`src/harnest/compatibility.py` aligned. The Go binary version comes from the
tag, while the wheel version comes from `pyproject.toml`; the installer
deliberately rejects a mismatch. Whenever framework support changes, update the
bounded `google-adk` and `langgraph` constraints together in `pyproject.toml`,
the Python compatibility matrix, and the Go init compatibility table. A release
machine needs Go 1.24 or newer, Python 3.10 or
newer, the Python `build` package, GoReleaser, and permission to publish to the
GitHub repository.

Run the full non-live checks and a local package build first:

```bash
make test
make example-test
goreleaser check
goreleaser release --snapshot --clean
```

Inspect the snapshot archives under `dist/`, including the wheel in `python/`.
To publish a clean `vX.Y.Z` tag:

```bash
GITHUB_TOKEN=... goreleaser release --clean
```

The release package is the standalone compiler, test runner, and agent server.
After installation, the normal local workflow is `harnest init`, `harnest
test`, `harnest compile`, `harnest serve`, and `harnest doctor`.

The Go binary also embeds the `harnest-authoring` coding-agent skill. Run
`harnest skills install` from a project root to install it for the detected
coding agent, or select `--target agents|codex|claude|cursor|copilot`. This
project-local guidance is separate from runtime skills authored under an
agent's `skills/` directory.
