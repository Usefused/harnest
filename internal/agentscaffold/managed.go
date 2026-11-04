// Package agentscaffold provides the shared minimal managed agent used by Harnest initializers.
package agentscaffold

import (
	"fmt"
	"strconv"
	"strings"
	"unicode"
)

// ManagedFiles returns editable starter sources without touching the destination project.
func ManagedFiles(name, framework string, port int) map[string]string {
	title := Title(name)
	return map[string]string{
		"agent.py": ManagedAgentSource(Identifier(name)),
		".gitignore": `.harnest/
.venv
__pycache__/
.pytest_cache/
`,
		"harnest.lock": `apiVersion: harnest.dev/v1alpha1
kind: ProjectLock
projectSchema: 6
`,
		"config.yaml": fmt.Sprintf(`apiVersion: harnest.dev/v1alpha1
kind: Agent
metadata:
  name: %s
  displayName: %s
spec:
  enabled: true
  entrypoint: agent:root_agent
  framework:
    name: %s
    mode: managed
  runtime:
    version: "3.12"
    dependencyFile: pyproject.toml
  # Set your OpenAI-compatible endpoint and model before making live calls.
  environment:
    OPENAI_MODEL: your-model
    OPENAI_BASE_URL: https://models.example.invalid/v1
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: NO_CONTENT
    ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS: "false"
`, name, title, framework),
		"agent-card.yaml": fmt.Sprintf(`name: %s
description: A self-contained Harnest agent.
version: 0.1.0
supportedInterfaces:
  - url: http://127.0.0.1:%d
    protocolBinding: HTTP+JSON
    protocolVersion: "1.0"
capabilities:
  streaming: true
defaultInputModes:
  - text/plain
defaultOutputModes:
  - text/plain
skills:
  - id: respond
    name: Respond
    description: Responds clearly to user requests.
    tags: [assistant]
`, title, port),
		"lifecycle/storage.py": `from harnest import lifecycle
from harnest.store import MemoryStore


@lifecycle.storage.sessions
@lifecycle.storage.checkpoints
def state_store():
    """Share one lifecycle-owned store without placing it in lib."""
    return MemoryStore()
`,
		"instructions.md": fmt.Sprintf(`You are %s.

Answer clearly, acknowledge uncertainty, and use discovered tools when they are relevant.
`, title),
		"pyproject.toml": agentPyproject(name),
	}
}

// Identifier returns a Python-safe form of a validated deployment name.
func Identifier(deploymentName string) string {
	return strings.ReplaceAll(deploymentName, "-", "_")
}

// Title renders a deployment name for the agent card.
func Title(deploymentName string) string {
	words := strings.Split(deploymentName, "-")
	for index, word := range words {
		if word == "" {
			continue
		}
		letters := []rune(word)
		letters[0] = unicode.ToUpper(letters[0])
		words[index] = string(letters)
	}
	return strings.Join(words, " ")
}

// agentPyproject describes the starter’s application dependencies.
func agentPyproject(name string) string {
	return fmt.Sprintf(`[project]
name = %s
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = []

[dependency-groups]
dev = ["pytest>=8,<9"]

[tool.uv]
package = false
`, strconv.Quote(name))
}

// ManagedAgentSource keeps model environment policy in the shared
// connector used by both runtime frameworks and evaluation models.
func ManagedAgentSource(agentName string) string {
	return fmt.Sprintf(`from harnest.agent import Agent
from harnest.model import LiteLLMModel


root_agent = Agent(
    name=%q,
    history="session",
    model=LiteLLMModel.from_openai_environment(),
)
`, agentName)
}
