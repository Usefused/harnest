package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAdvancedModeCheckReportsMigrationWithoutChangingFiles(t *testing.T) {
	target := filepath.Join(t.TempDir(), "managed-agent")
	if err := createScaffold(target, "managed-agent"); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(target, "config.yaml")
	agentPath := filepath.Join(target, "agent.py")
	configBefore, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	agentBefore, err := os.ReadFile(agentPath)
	if err != nil {
		t.Fatal(err)
	}

	stdout, _, err := executeForTest(
		t, defaultSystem(), "mode", "advanced", target, "--check",
	)
	if err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{
		"Advanced-mode migration check (read-only)",
		"Framework: adk",
		"Current mode: managed",
		"Entrypoint: agent:root_agent",
		"tools/",
		"extensions/",
		"plugins/",
		"skills/",
		"evals/",
		"Agent.advanced(...)",
		"spec.framework.mode to advanced",
		"No files were changed.",
	} {
		if !strings.Contains(stdout, expected) {
			t.Fatalf("advanced mode audit is missing %q:\n%s", expected, stdout)
		}
	}
	for _, placeholder := range []string{"  - subagents/", "  - mcp/", "  - sandbox/"} {
		if strings.Contains(stdout, placeholder) {
			t.Fatalf("advanced mode audit reported placeholder directory %q:\n%s", placeholder, stdout)
		}
	}

	configAfter, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	agentAfter, err := os.ReadFile(agentPath)
	if err != nil {
		t.Fatal(err)
	}
	if string(configAfter) != string(configBefore) || string(agentAfter) != string(agentBefore) {
		t.Fatal("advanced mode check modified config.yaml or agent.py")
	}
}

func TestAdvancedModeRequiresExplicitCheckFlag(t *testing.T) {
	target := filepath.Join(t.TempDir(), "managed-agent")
	if err := createScaffold(target, "managed-agent"); err != nil {
		t.Fatal(err)
	}
	_, _, err := executeForTest(t, defaultSystem(), "mode", "advanced", target)
	if err == nil || !strings.Contains(err.Error(), "pass --check") {
		t.Fatalf("got error %v, want actionable --check requirement", err)
	}
}

func TestAdvancedModeCheckReportsAlreadyAdvancedAgent(t *testing.T) {
	target := filepath.Join(t.TempDir(), "advanced-agent")
	if err := createScaffoldForMode(target, "advanced-agent", "langgraph", "advanced"); err != nil {
		t.Fatal(err)
	}
	stdout, _, err := executeForTest(
		t, defaultSystem(), "mode", "advanced", target, "--check",
	)
	if err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{
		"Framework: langgraph",
		"Current mode: advanced",
		"Managed resource directories requiring explicit wiring:\n  none",
		"Keep spec.framework.mode set to advanced",
	} {
		if !strings.Contains(stdout, expected) {
			t.Fatalf("advanced mode audit is missing %q:\n%s", expected, stdout)
		}
	}
}
