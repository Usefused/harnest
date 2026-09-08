package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestAddResourcesBuildsUpMinimalAgent exercises every incremental scaffold.
func TestAddResourcesBuildsUpMinimalAgent(t *testing.T) {
	root := filepath.Join(t.TempDir(), "incremental-agent")
	if _, _, err := executeForTest(t, defaultSystem(), "init", root, "--minimal"); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		kind     string
		name     string
		path     string
		expected string
	}{
		{"tool", "customer-lookup", "tools/customer_lookup.py", "@tool"},
		{"subagent", "researcher", "subagents/researcher.py", "researcher = Agent("},
		{"task", "prepare-report", "tasks/prepare_report.py", "@task(queue=\"default\""},
		{"lifecycle", "audit", "lifecycle/audit.py", "@lifecycle.agent.after"},
		{"context", "request-cache", "lifecycle/request_cache.py", "@context.provider(\"request_cache\")"},
	}
	for _, item := range cases {
		t.Run(item.kind, func(t *testing.T) {
			stdout, _, err := executeForTest(
				t, defaultSystem(), "add", item.kind, item.name, "--project", root,
			)
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(stdout, "Added "+item.kind) {
				t.Fatalf("unexpected add output: %q", stdout)
			}
			source := string(mustReadTestFile(t, filepath.Join(root, item.path)))
			if !strings.Contains(source, item.expected) {
				t.Fatalf("generated %s is missing %q:\n%s", item.path, item.expected, source)
			}
		})
	}

	_, _, err := executeForTest(
		t, defaultSystem(), "add", "tool", "customer-lookup", "--project", root,
	)
	if err == nil || !strings.Contains(err.Error(), "already exists") {
		t.Fatalf("expected duplicate resource error, got %v", err)
	}
}

// TestAddResourceDefaultsToCurrentAgent protects the documented agent-root workflow.
func TestAddResourceDefaultsToCurrentAgent(t *testing.T) {
	root := filepath.Join(t.TempDir(), "current-agent")
	if _, _, err := executeForTest(t, defaultSystem(), "init", root, "--minimal"); err != nil {
		t.Fatal(err)
	}
	t.Chdir(root)
	if _, _, err := executeForTest(t, defaultSystem(), "add", "tool", "lookup"); err != nil {
		t.Fatal(err)
	}
	source := string(mustReadTestFile(t, filepath.Join(root, "tools", "lookup.py")))
	if !strings.Contains(source, "from harnest.agent import tool") {
		t.Fatalf("generated tool uses a stale public import:\n%s", source)
	}
}

// TestAddResourceRejectsUnsafeNamesAndLinkedDirectories protects project scope.
func TestAddResourceRejectsUnsafeNamesAndLinkedDirectories(t *testing.T) {
	root := filepath.Join(t.TempDir(), "safe-agent")
	if _, _, err := executeForTest(t, defaultSystem(), "init", root, "--minimal"); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"../escape", "Class", "for"} {
		if _, _, err := executeForTest(
			t, defaultSystem(), "add", "tool", name, "--project", root,
		); err == nil {
			t.Fatalf("unsafe resource name %q was accepted", name)
		}
	}
	external := t.TempDir()
	if err := os.Symlink(external, filepath.Join(root, "tools")); err != nil {
		t.Fatal(err)
	}
	_, _, err := executeForTest(
		t, defaultSystem(), "add", "tool", "safe", "--project", root,
	)
	if err == nil || !strings.Contains(err.Error(), "symlink") {
		t.Fatalf("expected linked directory error, got %v", err)
	}
}

// TestAddResourceHonorsFrameworkOwnership avoids generating ignored resources.
func TestAddResourceHonorsFrameworkOwnership(t *testing.T) {
	advanced := filepath.Join(t.TempDir(), "advanced-agent")
	if _, _, err := executeForTest(
		t, defaultSystem(), "init", advanced, "--minimal", "--mode", "advanced",
	); err != nil {
		t.Fatal(err)
	}
	if _, _, err := executeForTest(
		t, defaultSystem(), "add", "tool", "lookup", "--project", advanced,
	); err == nil || !strings.Contains(err.Error(), "requires managed mode") {
		t.Fatalf("expected advanced ownership error, got %v", err)
	}

	langgraph := filepath.Join(t.TempDir(), "langgraph-agent")
	if _, _, err := executeForTest(
		t, defaultSystem(), "init", langgraph, "--minimal", "--framework", "langgraph",
	); err != nil {
		t.Fatal(err)
	}
	if _, _, err := executeForTest(
		t, defaultSystem(), "add", "subagent", "researcher", "--project", langgraph,
	); err == nil || !strings.Contains(err.Error(), "explicit Graph nodes") {
		t.Fatalf("expected LangGraph ownership error, got %v", err)
	}
}
