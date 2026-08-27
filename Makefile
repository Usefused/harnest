PYTHON ?= $(shell command -v python3.14 2>/dev/null || command -v python3.13 2>/dev/null || command -v python3.12 2>/dev/null || command -v python3.11 2>/dev/null || command -v python3.10 2>/dev/null || command -v python3)
GOCACHE ?= $(CURDIR)/.cache/go-build
LITELLM_API_BASE ?= http://127.0.0.1:11434
LITELLM_MODEL ?= ollama_chat/qwen3.5:cloud
COMPILED_HELPDESK ?= $(CURDIR)/.harnest/helpdesk
SERVE_HOST ?= 127.0.0.1
SERVE_PORT ?= 8080
SERVE_REQUEST_TIMEOUT ?= 300
SERVE_MAX_CONCURRENCY ?= 8
SERVE_EXTRA_ARGS ?=
AGENT_URL ?= http://127.0.0.1:$(SERVE_PORT)
DEMO_SESSION_ID ?= demo-session

.PHONY: test quality complexity format-check vet schemas plan dry-run validate-examples example-install compile-example serve-example demo-agent demo-session demo-response demo-stream example-test example-smoke example-eval example-all live-run live-test

test: schemas
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests/python -v
	GOCACHE=$(GOCACHE) go test ./...

quality: test complexity format-check vet validate-examples

complexity:
	$(PYTHON) scripts/check_python_complexity.py --max 10 src scripts tests/python examples/self-serve
	GOCACHE=$(GOCACHE) go tool gocyclo -over 10 cmd engine internal

format-check:
	@test -z "$$(gofmt -l cmd engine internal)" || (gofmt -l cmd engine internal; exit 1)

vet:
	GOCACHE=$(GOCACHE) go vet ./...

schemas:
	$(PYTHON) -m json.tool schemas/config.schema.json >/dev/null
	$(PYTHON) -m json.tool schemas/agent-card.schema.json >/dev/null
	$(PYTHON) -m json.tool schemas/deployment-plan.schema.json >/dev/null

plan:
	PYTHONPATH=src $(PYTHON) -m harnest.cli plan examples/self-serve/orchestrator.py

dry-run:
	PYTHONPATH=src GOCACHE=$(GOCACHE) go run ./cmd/harnest-runtime -python $(PYTHON) -orchestrator examples/self-serve/orchestrator.py

validate-examples: schemas plan dry-run example-test

example-install:
	$(PYTHON) -m pip install -e .

compile-example:
	LITELLM_API_BASE=$(LITELLM_API_BASE) LITELLM_MODEL=$(LITELLM_MODEL) PYTHONPATH=src $(PYTHON) -m harnest.cli compile examples/self-serve/agents/helpdesk --output $(COMPILED_HELPDESK)

serve-example: compile-example
	LITELLM_API_BASE=$(LITELLM_API_BASE) LITELLM_MODEL=$(LITELLM_MODEL) PYTHONPATH=src $(PYTHON) $(COMPILED_HELPDESK)/harnest-agent --host $(SERVE_HOST) --port $(SERVE_PORT) --request-timeout $(SERVE_REQUEST_TIMEOUT) --max-concurrency $(SERVE_MAX_CONCURRENCY) $(SERVE_EXTRA_ARGS)

demo-agent:
	curl -sS $(AGENT_URL)/agent

demo-session:
	curl -sS -X POST $(AGENT_URL)/sessions -H 'Content-Type: application/json' --data '{"id":"$(DEMO_SESSION_ID)","state":{}}'

demo-response:
	curl -sS -X POST $(AGENT_URL)/responses -H 'Content-Type: application/json' --data '{"input":"Triage a fictional production API authentication outage.","sessionId":"$(DEMO_SESSION_ID)"}'

demo-stream:
	curl -N -sS -X POST $(AGENT_URL)/responses -H 'Content-Type: application/json' --data '{"input":"What should I collect next?","sessionId":"$(DEMO_SESSION_ID)","stream":true}'

live-run: compile-example
	LITELLM_API_BASE=$(LITELLM_API_BASE) LITELLM_MODEL=$(LITELLM_MODEL) PYTHONPATH=src $(PYTHON) -m google.adk.cli run $(COMPILED_HELPDESK)

example-test:
	LITELLM_API_BASE=$(LITELLM_API_BASE) LITELLM_MODEL=$(LITELLM_MODEL) PYTHONPATH=src $(PYTHON) -m harnest.cli test examples/self-serve/agents/helpdesk

example-smoke:
	LITELLM_API_BASE=$(LITELLM_API_BASE) LITELLM_MODEL=$(LITELLM_MODEL) PYTHONPATH=src $(PYTHON) -m harnest.cli test examples/self-serve/agents/helpdesk --smoke

live-test: example-smoke

example-eval:
	LITELLM_API_BASE=$(LITELLM_API_BASE) LITELLM_MODEL=$(LITELLM_MODEL) PYTHONPATH=src $(PYTHON) -m harnest.cli test examples/self-serve/agents/helpdesk --evals

example-all:
	LITELLM_API_BASE=$(LITELLM_API_BASE) LITELLM_MODEL=$(LITELLM_MODEL) PYTHONPATH=src $(PYTHON) -m harnest.cli test examples/self-serve/agents/helpdesk --smoke --evals
