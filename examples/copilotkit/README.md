# [Harnest](https://usefused.com) × CopilotKit

A local compatibility demo built by Fused: one Harnest agent, a CopilotKit React UI,
and an AG-UI connection. Switch between Google ADK and LangGraph in the browser.
The agent is deterministic: no model API key or hosted CopilotKit account is
needed, and it does not generate open-ended answers.

## Prerequisites

- Python 3.11 or newer, with `venv` and `pip`.
- Node.js 22 and npm.
- A checkout of this repository.
- Free local ports **5173**, **1910**, and **1911**.

## Setup and run

From the repository root, create and activate a Python environment (or activate
an existing one), then install the framework dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[all]"

cd examples/copilotkit
npm ci
HARNEST_PYTHON="$(command -v python)" npm run dev
```

Open [the demo](http://127.0.0.1:5173). Wait for **Backend ready**, then choose a
framework and send a message. Keep the terminal running; **Ctrl+C** stops the UI
and both agent servers. To restart later, activate the same Python environment
and rerun the last command from this directory.

| Service | Address |
| --- | --- |
| CopilotKit UI | `http://127.0.0.1:5173` |
| Google ADK backend | `http://127.0.0.1:1910` |
| LangGraph backend | `http://127.0.0.1:1911` |

The UI proxies requests to each backend's `/agui` route. Both agents are compiled
from `agent/` using the production Harnest runtime. Generated artifacts go under
`.harnest/copilotkit/` at the repository root. Sessions are in memory and reset
when the backend restarts. **New conversation** starts an independent thread.

## Try the flows

| Action | Expected behavior |
| --- | --- |
| **Conversation**, or type a message | Stream a progress message and reply; update the shared turn count |
| **Browser tool** | Apply a violet page accent in the browser, return the tool result, and continue the agent |
| **Human approval** | Approve or decline a fictional publication and see the shared draft state |
| **Error recovery** | Show an intentional error; the next message can still succeed |
| Type `slow`, then **Stop run** | Cancel the request and send another message |

Expand **Inspect AG-UI events** to see the recent protocol events.

## Verify changes

From this directory, with the Python environment activated:

```bash
npx playwright install chromium
HARNEST_PYTHON="$(command -v python)" npm test
npm run build
```

The browser suite runs one complete journey per framework. It reuses a running
local demo or starts one automatically. The build checks TypeScript and creates
`dist/`; use `npm run dev` to run the full demo with both backend servers.

`node_modules/`, `dist/`, test output, and compiled agents are ignored by Git.
Only source, configuration, and the package manifest/lockfile are committed.

If Python imports fail, check that `HARNEST_PYTHON` points to the environment where
you installed Harnest. If startup reports a port conflict, stop the process using
that port before restarting the demo.

See the [Harnest serving documentation](https://usefused.com/docs/harnest/runtime/serving/neutral-api)
for the AG-UI API and interaction contracts.
