PYTHON ?= $(shell command -v python3.14 2>/dev/null || command -v python3.13 2>/dev/null || command -v python3.12 2>/dev/null || command -v python3.11 2>/dev/null || command -v python3.10 2>/dev/null || command -v python3)
GOCACHE ?= $(CURDIR)/.cache/go-build
OPENAI_BASE_URL ?= https://api.openai.com/v1
OPENAI_MODEL ?= gpt-4.1-mini
COMPILED_HELPDESK ?= $(CURDIR)/.harnest/helpdesk
AGENT_URL ?= http://127.0.0.1:8080
DEMO_SESSION_ID ?= demo-session

.PHONY: test quality complexity skill-quality format-check vet schemas plan dry-run validate-examples example-install compile-example serve-example demo-agent demo-session demo-response demo-stream example-test example-smoke example-eval example-all live-run live-test

test: schemas
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests/python -v
	GOCACHE=$(GOCACHE) go test ./...

quality: test complexity skill-quality format-check vet validate-examples

complexity:
	$(PYTHON) scripts/check_python_complexity.py --max 10 src scripts tests/python examples/self-serve
	GOCACHE=$(GOCACHE) go tool gocyclo -over 10 cmd engine internal

skill-quality:
	$(PYTHON) scripts/check_skill_quality.py --max-words 400 .

format-check:
	@test -z "$$(gofmt -l cmd engine internal)" || (gofmt -l cmd engine internal; exit 1)

vet:
	GOCACHE=$(GOCACHE) go vet ./...

schemas:
	$(PYTHON) -m json.tool schemas/config.schema.json >/dev/null
	$(PYTHON) -m json.tool schemas/project-lock.schema.json >/dev/null
	$(PYTHON) -m json.tool schemas/agent-card.schema.json >/dev/null
	$(PYTHON) -m json.tool schemas/deployment-plan.schema.json >/dev/null
	$(PYTHON) -m json.tool schemas/server.schema.json >/dev/null
	$(PYTHON) -m json.tool schemas/plugin.schema.json >/dev/null
	$(PYTHON) -m json.tool schemas/extension.schema.json >/dev/null
	$(PYTHON) -m json.tool schemas/eval-run-result.schema.json >/dev/null

plan:
	PYTHONPATH=src $(PYTHON) -m harnest.cli plan examples/self-serve/orchestrator.py

dry-run:
	PYTHONPATH=src GOCACHE=$(GOCACHE) go run ./cmd/harnest-runtime -python $(PYTHON) -orchestrator examples/self-serve/orchestrator.py

validate-examples: schemas plan dry-run example-test

example-install:
	$(PYTHON) -m pip install -e .

compile-example:
	OPENAI_BASE_URL=$(OPENAI_BASE_URL) OPENAI_MODEL=$(OPENAI_MODEL) PYTHONPATH=src $(PYTHON) -m harnest.cli compile examples/self-serve/agents/helpdesk --output $(COMPILED_HELPDESK)

serve-example: compile-example
	OPENAI_BASE_URL=$(OPENAI_BASE_URL) OPENAI_MODEL=$(OPENAI_MODEL) PYTHONPATH=src $(PYTHON) $(COMPILED_HELPDESK)/harnest-agent

demo-agent:
	curl -sS $(AGENT_URL)/agent

demo-session:
	curl -sS -X POST $(AGENT_URL)/sessions -H 'Content-Type: application/json' --data '{"id":"$(DEMO_SESSION_ID)","state":{}}'

demo-response:
	curl -sS -X POST $(AGENT_URL)/responses -H 'Content-Type: application/json' --data '{"input":"Triage a fictional production API authentication outage.","sessionId":"$(DEMO_SESSION_ID)"}'

demo-stream:
	curl -N -sS -X POST $(AGENT_URL)/responses -H 'Content-Type: application/json' --data '{"input":"What should I collect next?","sessionId":"$(DEMO_SESSION_ID)","stream":true}'

live-run: compile-example
	OPENAI_BASE_URL=$(OPENAI_BASE_URL) OPENAI_MODEL=$(OPENAI_MODEL) PYTHONPATH=src $(PYTHON) -m google.adk.cli run $(COMPILED_HELPDESK)

example-test:
	OPENAI_BASE_URL=$(OPENAI_BASE_URL) OPENAI_MODEL=$(OPENAI_MODEL) PYTHONPATH=src $(PYTHON) -m harnest.cli test examples/self-serve/agents/helpdesk

example-smoke:
	OPENAI_BASE_URL=$(OPENAI_BASE_URL) OPENAI_MODEL=$(OPENAI_MODEL) PYTHONPATH=src $(PYTHON) -m harnest.cli test examples/self-serve/agents/helpdesk --smoke

live-test: example-smoke

example-eval:
	OPENAI_BASE_URL=$(OPENAI_BASE_URL) OPENAI_MODEL=$(OPENAI_MODEL) PYTHONPATH=src $(PYTHON) -m harnest.cli test examples/self-serve/agents/helpdesk --evals

example-all:
	OPENAI_BASE_URL=$(OPENAI_BASE_URL) OPENAI_MODEL=$(OPENAI_MODEL) PYTHONPATH=src $(PYTHON) -m harnest.cli test examples/self-serve/agents/helpdesk --smoke --evals
