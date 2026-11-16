// Starter files that differ by agent framework. Adding a framework adds one frameworkScaffolds entry here.

package main

import (
	"fmt"

	"harnest.dev/harnest/internal/frameworks"
	"harnest.dev/harnest/internal/pysource"
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
	return map[string]string{"agent.py": pysource.Render("init/adk/advanced/agent.py.tmpl", map[string]any{"Name": identifier})}
}

// langGraphStarterFiles keeps subagents as explicit Graph nodes, because a
// LangGraph root agent does not consume the subagents/ folder directly.
func langGraphStarterFiles(files map[string]string, identifier, mode string) {
	delete(files, "subagents/__init__.py")
	files["subagents/_README.md"] = optionalFolderGuide("subagents", mode)
	files["evals/_README.md"] = optionalFolderGuide("evals", mode)
	files["tests/unit/test_agent.py"] = pysource.Render("init/langgraph/tests/unit/test_agent.py.tmpl", map[string]any{"Name": identifier})
}

// Advanced LangGraph passes the same lifecycle-owned store into native graph
// compilation, so the authored library module is required source.
func langGraphAdvancedFiles(identifier string) map[string]string {
	files := map[string]string{}
	// Advanced LangGraph passes the same lifecycle-owned store into native
	// graph compilation, so the authored library module is required source.
	files["lib/storage.py"] = pysource.File("init/langgraph/advanced/lib/storage.py")
	files["lifecycle/storage.py"] = pysource.File("init/langgraph/advanced/lifecycle/storage.py")
	files["agent.py"] = pysource.Render("init/langgraph/advanced/agent.py.tmpl", map[string]any{"Name": identifier})
	return files
}

func langGraphMinimalFiles(files map[string]string, mode string) {
	files["evals/_README.md"] = optionalFolderGuide("evals", mode)
}
