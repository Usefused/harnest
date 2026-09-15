package main

import (
	"path/filepath"
	"strings"
	"testing"
)

// TestMCPDelegatesConfiguredAgentPolicy keeps CLI reads on the agent interpreter.
func TestMCPDelegatesConfiguredAgentPolicy(t *testing.T) {
	target := filepath.Join(t.TempDir(), "mcp-agent")
	if err := createScaffold(target, "mcp-agent"); err != nil {
		t.Fatal(err)
	}
	record := filepath.Join(t.TempDir(), "arguments.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	python := writeExecutable(t, `#!/bin/sh
printf '%s\n' "$@" > "$HARNEST_TEST_RECORD"
`)
	_, _, err := executeForTest(t, defaultSystem(), "--python", python, "mcp", "prompt", "knowledge", "summarize", "--arg", "topic=a=b", "--project", target, "--json")
	if err != nil {
		t.Fatal(err)
	}
	arguments := string(mustReadTestFile(t, record))
	resolved, err := filepath.EvalSymlinks(target)
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "MCP delegated arguments", arguments, []string{"harnest.cli", "mcp\nprompt\nknowledge\nsummarize", "--arg\ntopic=a=b", "--project\n" + resolved, "--framework\nadk", "--json"})
}

// TestMCPDefaultsToCurrentAgentFolder pins the low-friction inspection workflow.
func TestMCPDefaultsToCurrentAgentFolder(t *testing.T) {
	root := newRootCommand(defaultSystem(), "test")
	command, _, err := root.Find([]string{"mcp", "inspect"})
	if err != nil {
		t.Fatal(err)
	}
	if command.Flags().Lookup("project").DefValue != "." {
		t.Fatal("MCP inspection must default to the current agent folder")
	}
	stdout, _, err := executeForTest(t, defaultSystem(), "mcp", "--help")
	if err != nil || !strings.Contains(stdout, "inspect") || !strings.Contains(stdout, "prompt") {
		t.Fatalf("MCP help is incomplete: %s, %v", stdout, err)
	}
}
