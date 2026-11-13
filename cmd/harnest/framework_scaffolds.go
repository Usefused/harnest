// Starter files that differ by agent framework. Adding a framework adds one frameworkScaffolds entry here.

package main

import (
	"fmt"

	"harnest.dev/harnest/internal/frameworks"
)

// frameworkScaffold owns the starter files that differ by agent framework.
// Shared starter files live in init.go; adding a framework means one entry in
// frameworkScaffolds plus its frameworks.json entry, and
// TestEveryFrameworkHasAScaffold keeps the two aligned.
type frameworkScaffold struct {
	// starter adjusts the guided and example starters after shared files exist.
	starter func(files map[string]string, identifier, mode string)
	// advanced returns the framework-specific agent.py and its support files.
	advanced func(identifier string) map[string]string
	// minimal adjusts the strict minimal starter.
	minimal func(files map[string]string, mode string)
}

var frameworkScaffolds = map[string]frameworkScaffold{
	"adk": {
		advanced: adkAdvancedFiles,
	},
	"langgraph": {
		starter:  langGraphStarterFiles,
		advanced: langGraphAdvancedFiles,
		minimal:  langGraphMinimalFiles,
	},
}

// scaffoldFor returns the selected framework's scaffold. Framework names are
// validated before scaffolding, so a missing entry is a registration defect.
func scaffoldFor(framework string) frameworkScaffold {
	scaffold, ok := frameworkScaffolds[framework]
	if !ok || !frameworks.Known(framework) {
		panic(fmt.Sprintf("framework %q has no init scaffold", framework))
	}
	return scaffold
}

// applyFrameworkStarterFiles runs the framework's starter adjustment, if any.
func applyFrameworkStarterFiles(files map[string]string, framework, identifier, mode string) {
	if starter := scaffoldFor(framework).starter; starter != nil {
		starter(files, identifier, mode)
	}
}

func adkAdvancedFiles(identifier string) map[string]string {
	return map[string]string{"agent.py": fmt.Sprintf(`from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.apps.app import ResumabilityConfig
from harnest.agent import Agent
from harnest.model import LiteLLMModel


# Advanced mode keeps Harnest's neutral server/auth boundaries, while this file
# owns native graph, checkpoint, middleware, and arbitrary model-call behavior.
root_agent = Agent.advanced(
    name=%q,
    target=App(
        name=%q,
        resumability_config=ResumabilityConfig(is_resumable=True),
        root_agent=LlmAgent(
            name=%q,
            model=LiteLLMModel.from_openai_environment().build(),
            instruction="You are a clear and helpful assistant.",
        ),
    )
)
`, identifier, identifier, identifier)}
}

// langGraphStarterFiles keeps subagents as explicit Graph nodes, because a
// LangGraph root agent does not consume the subagents/ folder directly.
func langGraphStarterFiles(files map[string]string, identifier, mode string) {
	delete(files, "subagents/__init__.py")
	files["subagents/_README.md"] = optionalFolderGuide("subagents", mode)
	files["evals/_README.md"] = optionalFolderGuide("evals", mode)
	files["tests/unit/test_agent.py"] = fmt.Sprintf(`def test_agent_name(agent, tools):
    assert agent.name == %q
    assert tools["echo"]("hello") == "hello"
`, identifier)
}

// Advanced LangGraph passes the same lifecycle-owned store into native graph
// compilation, so the authored library module is required source.
func langGraphAdvancedFiles(identifier string) map[string]string {
	files := map[string]string{}
	// Advanced LangGraph passes the same lifecycle-owned store into native
	// graph compilation, so the authored library module is required source.
	files["lib/storage.py"] = `from harnest.store import MemoryStore


store = MemoryStore()
`
	files["lifecycle/storage.py"] = `from harnest import lifecycle
from harnest.lib.storage import store


@lifecycle.storage.sessions
@lifecycle.storage.checkpoints
def state_store():
    """Share the store passed to the native LangGraph checkpointer."""
    return store
`
	files["agent.py"] = fmt.Sprintf(`from langchain.agents import create_agent
from harnest.agent import Agent
from harnest.lib.storage import store
from harnest.model import LiteLLMModel


# Advanced mode keeps Harnest's neutral server/auth boundaries, while this file
# owns native graph, checkpoint, middleware, and arbitrary model-call behavior.
root_agent = Agent.advanced(
    name=%q,
    target=create_agent(
        model=LiteLLMModel.from_openai_environment().build(),
        tools=[],
        system_prompt="You are a clear and helpful assistant.",
        name=%q,
        checkpointer=store.framework_checkpointer(),
    )
)
`, identifier, identifier)
	return files
}

func langGraphMinimalFiles(files map[string]string, mode string) {
	files["evals/_README.md"] = optionalFolderGuide("evals", mode)
}
