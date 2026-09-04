# Harnest Extension examples

These directories are reusable Harnest Extension sources rather than complete
agents. Copy the selected directory into an agent's `extensions/` folder and add
its SDK dependencies to that agent's root `pyproject.toml`.

`hatchet/` demonstrates an agentic adapter for an independently deployed
Hatchet runtime. It intentionally contributes no Harnest tools: the consuming
agent defines its own domain tool and calls `harnest.extensions.hatchet.hatchet`.

The live infrastructure fixture is under `tests/fixtures/hatchet/`. It keeps
Hatchet and its worker in Docker so stopping Harnest cannot stop an external
job.
