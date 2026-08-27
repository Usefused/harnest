# Installation and releases

## Install the standalone CLI

Harnest releases contain a native `harnest` executable with the matching Python
wheel and native `uv` bootstrapper embedded directly inside it. The installer
supports macOS and Linux on AMD64 and ARM64 and does not require a preinstalled
Python. Normal Harnest commands use the installed managed environment
automatically.

Install the latest release from the canonical repository:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/creativeJoe007/harnest/main/install.sh |
  sh
```

The installer prints the resolved version, CLI destination, and managed-runtime
destination and requires an explicit `y` confirmation before downloading or
installing anything. It fails closed when no terminal is available. CI and
other deliberately non-interactive callers must opt in explicitly:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/creativeJoe007/harnest/main/install.sh |
  HARNEST_YES=1 sh
```

For a pinned version or a fork, set variables on the `sh` process receiving the
script:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/creativeJoe007/harnest/main/install.sh |
  HARNEST_VERSION=v0.1.14 HARNEST_REPO=creativeJoe007/harnest sh
```

`HARNEST_VERSION` accepts a release version with or without the leading `v`.
When omitted, the installer follows the repository's latest GitHub Release.
`HARNEST_REPO` defaults to `creativeJoe007/harnest` and accepts an
`owner/repository` override. Set `GITHUB_TOKEN` for a private fork; the installer
does not print it.

The installer downloads the platform archive and `checksums.txt` from the same
GitHub Release, requires a matching SHA-256 entry, and verifies the archive
before extracting it. The executable rejects its embedded wheel if the wheel
metadata differs from the CLI version. The installer then:

- creates or updates a dedicated virtual environment at
  `${HARNEST_RUNTIME_DIR:-$HOME/.harnest/runtime}`;
- asks the executable to install its embedded wheel and Python dependencies;
  and
- atomically installs the native executable at
  `${HARNEST_INSTALL_DIR:-$HOME/.local/bin}/harnest`.

The installer checks versioned Python commands from 3.14 down to 3.10, then
`python3` and `python`, and uses the first interpreter that actually reports
Python 3.10 or newer. This avoids selecting an older operating-system `python3`
when a supported Homebrew, pyenv, or other installation is present. If none is
compatible, embedded `uv` installs pinned CPython 3.12 under the Harnest data
directory and creates the runtime from it. Harnest does not modify the system
Python installation. `HARNEST_BOOTSTRAP_PYTHON` requires an exact host
interpreter instead of allowing the managed fallback.

`HARNEST_PYTHON` is different: it overrides the interpreter used later by the
`harnest` command. The CLI normally resolves Python in this order: `--python`,
`HARNEST_PYTHON`, the managed `HARNEST_RUNTIME_DIR`, then `python3` on `PATH`.

Add the install directory to `PATH` if necessary, then verify both layers:

```bash
harnest --version
harnest doctor
```

The managed runtime is private implementation state. Users run the native CLI;
they do not activate the runtime or call `python -m harnest.cli`. When the
installer finds Harnest's retired Python launcher in a writable directory, it
replaces that launcher in place so `harnest` resolves correctly immediately.
An explicit `HARNEST_INSTALL_DIR` is always respected. For other collisions or
non-writable locations, the installer prints one PATH setup command before the
normal, portable `harnest ...` workflow.

Rerunning the installer upgrades the managed runtime from the new executable
before atomically replacing the installed executable. For a review-first
installation, download `install.sh`, inspect it, then execute the local file
with the same environment variables.

Harnest releases also define the supported ADK and LangGraph version ranges.
Those bounds are published in `pyproject.toml`, installed with the matching
wheel, and copied into newly scaffolded agent requirements. Compilation checks
the installed selected framework against the release's bounds in both managed
and advanced mode. To use a newer framework outside them, install a newer
Harnest release that declares support before changing the agent dependency.
Direct ADK or LangGraph imports in advanced mode do not bypass this check.

## Publish a release

Releases use GoReleaser v2.18 or newer. Each platform archive contains:

- the native `harnest` executable built from `cmd/harnest`, including its
  version-matched universal Python wheel and platform-native `uv` bootstrapper;
- `THIRD_PARTY_NOTICES.md` and the redistributed `uv` license;
- `README.md`; and
- the Apache 2.0 `LICENSE`.

GoReleaser also publishes `checksums.txt` with SHA-256 checksums for every
archive. Releases are explicitly targeted at `creativeJoe007/harnest`, matching
the installer's default source.

Before tagging, ensure the static version in `pyproject.toml` exactly matches
the tag without its leading `v`, and keep the source-tree fallback in
`src/harnest/compatibility.py` aligned. The Go binary version comes from the
tag, while the wheel version comes from `pyproject.toml`; GoReleaser builds the
wheel before compiling it into the binary, which deliberately rejects a
mismatch at runtime. Whenever framework support changes, update the
bounded `google-adk` and `langgraph` constraints together in `pyproject.toml`,
the Python compatibility matrix, and the Go init compatibility table. A release
machine needs Go 1.24 or newer, Python 3.10 or newer, the Python `build`
package, GoReleaser, and permission to download the pinned `uv` assets and
publish to the GitHub repository.

Run the full non-live checks and a local package build first:

```bash
make test
make example-test
goreleaser check
goreleaser release --snapshot --clean
```

Inspect the snapshot archives under `dist/` and run `harnest runtime install`
from an extracted binary to verify its embedded wheel.
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
An existing project-local copy is intentionally preserved across Harnest
upgrades. Review local customizations, then run `harnest skills install --force`
when the project should receive the release's updated authoring guidance.

## GitHub Actions

`.github/workflows/ci.yml` runs the quality matrix for pull requests and branch
pushes. After a successful `main` run, its release-tag job reads
`project.version` from `pyproject.toml` and creates the matching `v<version>`
tag. It refuses to move an existing tag, so bump `project.version` before the
next release-bearing push.

`.github/workflows/release.yml` is a separate packaging pipeline. It runs after
successful CI on `main`, verifies that CI's exact commit owns the version tag,
and then uses GoReleaser to publish the platform archives with the matching
Python wheel embedded in each executable, plus checksums. Separating
validation/tagging from packaging makes failed
release builds rerunnable without weakening the immutable-tag check.
