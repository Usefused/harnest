package main

import (
	"testing"

	"harnest.dev/harnest/internal/frameworks"
)

// Every framework in frameworks.json must ship an advanced starter, and every
// scaffold entry must name a registered framework.
func TestEveryFrameworkHasAScaffold(t *testing.T) {
	for _, name := range frameworks.Names() {
		scaffold, ok := frameworkScaffolds[name]
		if !ok || scaffold.advanced == nil {
			t.Fatalf("framework %q has no advanced init scaffold", name)
		}
		if _, ok := scaffold.advanced("demo_agent")["agent.py"]; !ok {
			t.Fatalf("framework %q advanced scaffold does not write agent.py", name)
		}
	}
	for name := range frameworkScaffolds {
		if !frameworks.Known(name) {
			t.Fatalf("scaffold %q is not registered in frameworks.json", name)
		}
	}
}
