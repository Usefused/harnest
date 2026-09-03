# Harnest eval showcase

This example exercises every evaluation metric in the Google ADK version
supported by this Harnest checkout. It uses one small city-facts agent design
in two independently runnable projects:

- `reference-agent/` covers golden-response, tool-trajectory, safety,
  hallucination, rubric, multi-turn, and custom metrics.
- `simulation-agent/` covers LLM-backed user simulation and
  `per_turn_user_simulator_quality_v1`.

The reference lane configures these remaining ADK 2.8 built-ins:

- `tool_trajectory_avg_score`
- `response_evaluation_score`
- `response_match_score`
- `safety_v1`
- `final_response_match_v2`
- `rubric_based_final_response_quality_v1`
- `hallucinations_v1`
- `rubric_based_tool_use_quality_v1`
- `multi_turn_task_success_v1`
- `multi_turn_trajectory_quality_v1`
- `multi_turn_tool_use_quality_v1`
- `rubric_based_multi_turn_trajectory_quality_v1`

It also includes `verified_capital_present`, an authored Python custom metric.
Harnest reflects the installed ADK registry rather than freezing an allowlist,
so check the playground catalog after an ADK upgrade for newly added metrics.

The split is required by the ADK format: one eval case contains either a static
golden `conversation` or a `conversationScenario`, and each Harnest agent has
one shared `evals/test_config.json`.

The reference case is a two-turn context test in one evaluator-owned session.
Its first turn verifies Paris through `get_city_fact`; its follow-up checks the
answer against the prior conversation and requires no redundant tool call.

## Credentials

Both agents use `LiteLLMModel.from_openai_environment()`. Omitted judge and
simulator model IDs use the same `OPENAI_MODEL`, defaulting to `gpt-4.1-mini`.
Set `OPENAI_API_KEY` in the process environment before local `harnest test`,
`run`, or `serve` commands. `OPENAI_BASE_URL` selects an OpenAI-compatible
endpoint, including Ollama's compatible API; no `OLLAMA_API_KEY` is needed.
Edit the non-secret model and endpoint values in each `config.yaml` when using
a different backend, because `spec.environment` overrides matching shell values.

Harnest does not load `.env` files. The illustrative `spec.secrets` entry maps
`OPENAI_API_KEY` for deployment only; local commands do not resolve its
`secretRef`.

The following metrics use Vertex AI's Gen AI evaluation service:

- `response_evaluation_score`
- `safety_v1`
- `multi_turn_task_success_v1`
- `multi_turn_trajectory_quality_v1`
- `multi_turn_tool_use_quality_v1`

These are separate Google service backends: `OPENAI_API_KEY` does not
authenticate them. The installed ADK evaluation client uses `GOOGLE_API_KEY`
when it is set.
Alternatively, set `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` and make
Application Default Credentials available.

```bash
export OPENAI_API_KEY="..."
export GOOGLE_API_KEY="..."  # Reference lane's Google services; ADC is an alternative.
```

See the canonical [model credential process](https://docs.usefused.com/harnest/build/project-configuration#configure-model-credentials). Never put credential values in `spec.environment`, agent source, or `evals/test_config.json`.

## Run every metric

From the repository root:

```bash
harnest test examples/evals-showcase/reference-agent --evals
harnest test examples/evals-showcase/simulation-agent --evals
```

Use `--eval-trajectory strict` on the reference lane when no additional tool
calls should be allowed. The default `business` policy requires the authored
calls in order but permits extra discovery calls.

Eval execution calls live models and can consume paid capacity. Harnest runs
each suite once. Judge metrics that support repeated sampling set `numSamples`
to `1` in this example to keep the demonstration economical; raise it when
judge stability matters more than cost.

## Author a golden result

The expected result belongs inside an `evals/*.evalset.json` invocation:

```json
{
  "userContent": {"role": "user", "parts": [{"text": "The prompt"}]},
  "finalResponse": {"role": "model", "parts": [{"text": "Expected answer"}]},
  "intermediateData": {
    "toolUses": [{"name": "get_city_fact", "args": {"city": "Paris"}}],
    "toolResponses": []
  }
}
```

`finalResponse` is the golden response. `intermediateData.toolUses` is the
golden tool trajectory. Add multiple invocation objects to `conversation` for
a multi-turn result, and place reusable judge properties in `rubrics`.

## Save an actual eval result

The CLI prints a complete structured JSON `EvalRunResult` by default. Write the
same payload atomically to a selected file with `--eval-output`:

```bash
harnest test examples/evals-showcase/reference-agent --evals \
  --eval-output reference-eval-result.json
```

Use `--no-output` when only the file and exit status are needed:

```bash
harnest test examples/evals-showcase/reference-agent --evals --no-output \
  --eval-output reference-eval-result.json
```

The result contains every suite and case, actual and expected invocations,
overall and per-invocation metric results including rubric details, and session
details. Keep the selected file as a CI artifact if historical comparisons are
needed; Harnest does not maintain implicit eval-result history.
