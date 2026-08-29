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
  https://raw.githubusercontent.com/Usefused/harnest/main/install.sh |
  sh
```

The installer prints the resolved version, CLI destination, and managed-runtime
destination and requires an explicit `y` confirmation before downloading or
installing anything. It fails closed when no terminal is available. CI and
other deliberately non-interactive callers must opt in explicitly:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/Usefused/harnest/main/install.sh |
  HARNEST_YES=1 sh
```

For a pinned version or a fork, set variables on the `sh` process receiving the
script:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/Usefused/harnest/main/install.sh |
  HARNEST_VERSION=v0.1.18 HARNEST_REPO=Usefused/harnest sh
```

`HARNEST_VERSION` accepts a release version with or without the leading `v`.
When omitted, the installer follows the repository's latest GitHub Release.
`HARNEST_REPO` defaults to `Usefused/harnest` and accepts an
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

That private CLI runtime is not shared with authored agents. Released
`compile`, `test`, and `serve` commands use the embedded `uv` and wheel to
synchronize each agent's `pyproject.toml` into a fingerprinted environment
below its `.harnest/` directory. Commit the resulting `uv.lock`. CI should run
`harnest env sync AGENT_DIR --frozen` to reject a missing or stale lock.

Rerunning the installer upgrades the managed runtime from the new executable
before atomically replacing the installed executable. For a review-first
installation, download `install.sh`, inspect it, then execute the local file
with the same environment variables.

Upgrading the Harnest installation and upgrading an authored agent repository
are separate operations. After installing a newer CLI, inspect an older agent's
filesystem migration without changing it:

```bash
harnest upgrade existing-agent
```

Resolve any reported manual blockers, commit or otherwise preserve current
work, and then opt into the planned rewrites and moves:

```bash
harnest upgrade existing-agent --apply
```

The apply command prints its fresh effective plan, verifies its source hashes,
and writes recovery copies under `.harnest/upgrade-backups/<id>/` before
touching authored files. The
committed `harnest.lock` records the project schema, not the installed CLI or
framework package version. Repositories that predate lifecycle-owned storage
receive the minimal shared `MemoryStore` in `lib/storage.py` and the required
session-store and checkpointer factories in `extensions/storage.py`. Existing
partial or custom storage is reported as a manual blocker rather than guessed.
Review the resulting diff, run `harnest test`, and compile before deployment.

Harnest releases also define the supported ADK and LangGraph version ranges.
Those bounds are published in Harnest's `pyproject.toml`, installed with the
matching wheel, and deliberately omitted from scaffolded agent projects.
Environment synchronization rejects an agent declaration of a compiler-owned
framework package. Compilation checks
the installed selected framework against the release's bounds in both managed
and advanced mode. To use a newer framework, install a newer Harnest release
that declares support; do not change the agent dependency project.
Direct ADK or LangGraph imports in advanced mode do not bypass this check.

## Publish a release

Releases use GoReleaser v2.18 or newer. Each platform archive contains:

- the native `harnest` executable built from `cmd/harnest`, including its
  version-matched universal Python wheel and platform-native `uv` bootstrapper;
- `THIRD_PARTY_NOTICES.md` and the redistributed `uv` license;
- `README.md`; and
- the Apache 2.0 `LICENSE`.

GoReleaser also publishes `checksums.txt` with SHA-256 checksums for every
archive. Releases are explicitly targeted at `Usefused/harnest`, matching
the installer's default source.

Release Please derives semantic versions from Conventional Commit messages and
maintains a reviewable release PR. `fix:` produces a patch, `feat:` produces a
minor, and a `!` or `BREAKING CHANGE:` produces a major. Documentation, tests,
and maintenance commits do not release by themselves. The release PR updates
`pyproject.toml`, `src/harnest/compatibility.py`, the release manifest, and the
changelog together. Merging it creates the matching tag and GitHub Release.

GoReleaser refuses to package a tag unless both checked-in version sources match
it. It builds the embedded wheel from disposable staged inputs; this preserves
the reviewed source version for releases while allowing local snapshot versions
without modifying the checkout. Whenever framework support changes, update the
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
from an extracted binary to verify its embedded wheel. Normal publication is
performed by merging the Release Please PR; do not create release tags by hand.
To retry artifact packaging for an existing Release Please release, dispatch
the `Release` workflow with its `vX.Y.Z` tag and, optionally, its expected SHA.

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
pushes but never changes versions or tags commits. After a successful `main`
run, `.github/workflows/release-please.yml` opens or updates the release PR. Set
the `RELEASE_PLEASE_TOKEN` repository secret to a fine-grained token with
contents, issues, and pull-request write access so GitHub runs normal CI on that
generated PR. The token is required because GitHub suppresses workflows caused
by `GITHUB_TOKEN`, including the published-release event that starts packaging.

When the release PR is merged and its `main` CI run succeeds, Release Please
creates the version tag and GitHub Release. That published Release independently
starts `.github/workflows/release.yml`, which verifies the tag, source versions,
and existing release before GoReleaser attaches platform archives and checksums.
A manual dispatch reruns packaging without moving the tag or replacing Release
Please's release notes.
