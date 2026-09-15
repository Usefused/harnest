package main

import (
	"fmt"
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
	assertContainsAll(t, "added subagent model", string(mustReadTestFile(t, filepath.Join(root, "subagents", "researcher.py"))), []string{
		"from harnest.model import LiteLLMModel", "model=LiteLLMModel.from_openai_environment()",
	})

	_, _, err := executeForTest(
		t, defaultSystem(), "add", "tool", "customer-lookup", "--project", root,
	)
	if err == nil || !strings.Contains(err.Error(), "already exists") {
		t.Fatalf("expected duplicate resource error, got %v", err)
	}
}

// TestAddMCPScaffoldsAuthChoices validates secure remote-client generation and flags.
func TestAddMCPScaffoldsAuthChoices(t *testing.T) {
	root := filepath.Join(t.TempDir(), "mcp-agent")
	if _, _, err := executeForTest(t, defaultSystem(), "init", root, "--minimal"); err != nil {
		t.Fatal(err)
	}
	stdout, _, err := executeForTest(
		t, defaultSystem(), "add", "mcp", "orders", "--project", root,
		"--url", "http://127.0.0.1:28081/mcp/orders",
		"--token-env", "ORDERS_MCP_TOKEN",
	)
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "default MCP output", stdout, []string{
		`Added mcp "orders"`, "export ORDERS_MCP_TOKEN", "harnest test .",
	})
	orders := string(mustReadTestFile(t, filepath.Join(root, "mcp", "orders.py")))
	assertContainsAll(t, "default MCP source", orders, []string{
		"from harnest.mcp import MCPClient",
		"MCPClient.streamable_http(",
		`"http://127.0.0.1:28081/mcp/orders"`,
		`headers={"Authorization": "Bearer ${ORDERS_MCP_TOKEN}"}`,
		`prefix="orders"`,
	})

	_, _, err = executeForTest(
		t, defaultSystem(), "add", "mcp", "internal", "--project", root,
		"--url", "https://mcp.example.com/events", "--transport", "sse",
		"--token-env", "INTERNAL_KEY", "--token-header", "X-API-Key",
		"--token-prefix=",
	)
	if err != nil {
		t.Fatal(err)
	}
	internal := string(mustReadTestFile(t, filepath.Join(root, "mcp", "internal.py")))
	assertContainsAll(t, "custom MCP source", internal, []string{
		"MCPClient.sse(", `headers={"X-API-Key": "${INTERNAL_KEY}"}`,
	})

	_, _, err = executeForTest(
		t, defaultSystem(), "add", "mcp", "public", "--project", root,
		"--url", "https://mcp.example.com/public",
	)
	if err != nil {
		t.Fatal(err)
	}
	public := string(mustReadTestFile(t, filepath.Join(root, "mcp", "public.py")))
	if strings.Contains(public, "headers=") {
		t.Fatalf("unauthenticated MCP source unexpectedly contains headers:\n%s", public)
	}

	invalid := [][]string{
		{},
		{"--url", "ftp://mcp.example.com"},
		{"--url", "https://secret@mcp.example.com", "--token-env", "TOKEN"},
		{"--url", "https://mcp.example.com", "--transport", "websocket"},
		{"--url", "https://mcp.example.com", "--token-env", "actual-secret"},
		{"--url", "https://mcp.example.com", "--token-env", "TOKEN", "--token-header", "Bad Header"},
		{"--url", "https://mcp.example.com", "--token-header", "X-API-Key"},
	}
	for index, arguments := range invalid {
		command := []string{"add", "mcp", fmt.Sprintf("invalid-%d", index), "--project", root}
		command = append(command, arguments...)
		if _, _, err := executeForTest(t, defaultSystem(), command...); err == nil {
			t.Fatalf("invalid MCP arguments were accepted: %v", arguments)
		}
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
	if _, _, err := executeForTest(
		t, defaultSystem(), "add", "mcp", "catalog", "--project", advanced,
		"--url", "https://mcp.example.com/mcp",
	); err == nil || !strings.Contains(err.Error(), "requires managed mode") {
		t.Fatalf("expected advanced MCP ownership error, got %v", err)
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
