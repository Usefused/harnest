package main

import (
	"strings"
	"testing"

	"harnest.dev/harnest/internal/frameworks"
	"harnest.dev/harnest/internal/pysource"
)

// Every framework in frameworks.json must ship an advanced starter, and every
// scaffold folder must name a registered framework.
func TestEveryFrameworkHasAScaffold(t *testing.T) {
	for _, name := range frameworks.Names() {
		if _, ok := frameworkScaffoldFiles(name, "advanced", "demo_agent")["agent.py"]; !ok {
			t.Fatalf("framework %q has no %s%s/advanced/agent.py", name, frameworkScaffoldRoot, name)
		}
	}
	for _, file := range pysource.Names(frameworkScaffoldRoot) {
		framework, rest, _ := strings.Cut(strings.TrimPrefix(file, frameworkScaffoldRoot), "/")
		if !frameworks.Known(framework) {
			t.Fatalf("scaffold folder %q is not registered in frameworks.json", framework)
		}
		if kind, _, _ := strings.Cut(rest, "/"); kind != "advanced" && kind != "starter" {
			t.Fatalf("%s must sit under advanced/ or starter/", file)
		}
	}
}

func TestFrameworkScaffoldFilesFillTheAgentName(t *testing.T) {
	files := frameworkScaffoldFiles("langgraph", "starter", "demo_agent")
	if !strings.Contains(files["tests/unit/test_agent.py"], `assert agent.name == "demo_agent"`) {
		t.Fatalf("LangGraph starter test was not rendered: %#v", files)
	}
}
