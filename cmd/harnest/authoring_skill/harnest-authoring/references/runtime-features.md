# Scheduling, private input, evaluation, and AG-UI

Read this alongside [python-api.md](python-api.md) for current runtime authoring.
These contracts apply to ADK and LangGraph unless a project uses explicit native
wiring in advanced mode.

## Scheduled tasks

Scaffold a fixed schedule with `harnest add cron daily-report --schedule "0 9 * * 1-5"`,
or a dynamic target with `harnest add cron reminder --dynamic`. Both create only
`cron/<name>.py`; configure shared task/cron storage before serving.

For new work, put one same-named function in `cron/daily_report.py`:

```python
from harnest.cron import cron


@cron("0 9 * * 1-5", arguments={"account_id": "acct_123"}, queue="reports", max_retries=3)
async def daily_report(account_id: str):
    """Run idempotent report work through the durable queue."""
    # Replace with the application's real report operation.
    raise NotImplementedError("Implement report generation")
```

The decorator creates the task; never stack `@task`. Direct calls execute inline.
For a dynamic target, use `@cron(queue="reports")` without a schedule or arguments.
Within runtime code, create a user-owned schedule with
`await harnest.cron.create(key="daily", expression="0 9 * * *", task="daily_report", arguments={"account_id": "acct_123"})`.
Named targets must be deployed cron functions. `get(id)`, `list(after=..., limit=...)`,
`update(id, expression=..., arguments=...)`, `pause(id)`, `resume(id)`, and `cancel(id)`
are async APIs scoped to the active user. They require a serving runtime, not a
compiler import or Studio process. Static schedules remain application-owned.

Expressions have five numeric UTC fields. Register the **same provider instance**
under `@lifecycle.storage.tasks` and `@lifecycle.storage.cron`. One stacked factory
is sufficient. `MemoryTaskStore` from `harnest.task` is local-only. For persistence,
use `PostgresTaskStore` from `harnest.task`, `PostgresStore` from `harnest_postgres`,
or `RedisStore` from `harnest_redis`. The latter two also support sessions and
checkpoints. The session-only providers in `harnest.store` do not implement task
storage. Managed environments include the database drivers.
Read URLs from environment variables inside the factory. Keep one provider per
role, and ensure task side effects are idempotent because delivery can retry.

Existing `Cron(...)` declarations targeting `@task` remain valid. Do not migrate
them automatically: replacing the task with a cron function changes its identity.

## Private client input

Use `from harnest.agent import client_input` and decorate an async function
in `tools/<name>.py` with
`@client_input(input_schema=PrivateInput, response={"status": "accepted"})`.
`PrivateInput` must be a Pydantic model. The handler's first positional parameter
receives its validated value and is excluded from the public tool signature;
remaining parameters are public arguments. The handler returns `None`. Only the
authored static JSON `response` reaches the model after success.

Implement the consuming operation before serving. Never put private values in
tool results, logs, exceptions, session state, task arguments, or schema defaults.
Do not combine this decorator with a durable tool. The pending continuation is
process-local, with `timeout_seconds=300` by default. The client receives a
`client_tool` action with `privateInput: true` and `inputSchema`; submit its value
through the existing client-tool result transport. Schema metadata itself is public.

## Evaluation metrics

`harnest add eval NAME --metric ID` scaffolds an EvalSet and merges the selected
criterion into `evals/test_config.json`, preserving existing criteria. Use
`--list-metrics` for the current preset catalog or `--i` for interactive selection.
Presets are authoring shortcuts, not a runtime metric allowlist. They work with
ADK and LangGraph. Judge/simulator presets need a model; Vertex presets need the
Vertex evaluation service. Scoring does not remove the agent's own model calls.

`--metric custom` creates a bare scorer in `lib/NAME.py`. Import `MetricContext`,
`MetricScore`, and `metric` from `harnest.evaluation`. A sync or async `@metric`
function accepts `MetricContext` (`actual`, `expected`, `scenario`, `metric`,
`threshold`) and returns `MetricScore(score, evidence)`, one score per actual
invocation, or a native ADK EvaluationResult. `score=None` means unscored.
Register its `harnest.lib.NAME.NAME` import in `customMetrics` and add its ID to
`criteria`. Never use a constant passing score as an implemented evaluator.
Run with `harnest test . --evals`; see [workflows.md](workflows.md) for result files
and business versus strict trajectory matching.

## AG-UI clients

The serving runtime exposes `POST /agui` as an SSE bridge to the same invocation
coordinator used by native transports. Send `threadId`, `runId`, `messages`,
`state`, `tools`, and `context` in the AG-UI envelope. A new text run ends with a
user message; an empty `messages` list initializes a thread without invoking a
model. Consume typed lifecycle, message, tool-call, and state events.

Client tool descriptors and forwarded properties never grant execution authority;
authentication and declared server tools remain authoritative. Follow the shared
approval/client-tool continuation contracts; do not resubmit client transcripts as
trusted history. Select AG-UI in Playground to test the protocol and complete
batched approvals/client inputs. Private resumes omit messages and state.

Set `server.agui: true` or `false` in `config.yaml`; it defaults to true for
compatibility. Studio's AG-UI transport component proposes the same setting.
Rebuild and restart after applying configuration changes.

Use `from harnest import ui` and `await ui.emit("app.progress", {"step": 1})`
inside an async tool or graph node to send an application-owned UI event. It
becomes AG-UI `CUSTOM` with the exact name/value, or native `ui_event` output.
Names are 1–128 characters; `harnest.*` and runtime control names are reserved.
Values must be finite JSON, at most 64 KiB each, with 128 events and 512 KiB per
invocation. Emit public display data only; events are transient and do not enter
model history. Streamed emissions apply backpressure and arrive during tool work.
Use `Event(state_delta={...})` for authored graph state; UI events do not mutate
session state. Playground displays JSON safely; application frontends own widgets.
Studio's Custom UI event component scaffolds an async tool using this contract.
