package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAdvancedModeCheckReportsMigrationWithoutChangingFiles(t *testing.T) {
	target := filepath.Join(t.TempDir(), "managed-agent")
	if err := createExampleScaffoldForMode(
		target, "managed-agent", "adk", "managed",
	); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(
		filepath.Join(target, "lib", "shared.py"),
		[]byte("def shared():\n    return 'shared'\n"),
		0o644,
	); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(target, "config.yaml")
	agentPath := filepath.Join(target, "agent.py")
	configBefore := mustReadTestFile(t, configPath)
	agentBefore := mustReadTestFile(t, agentPath)

	stdout, _, err := executeForTest(
		t, defaultSystem(), "mode", "advanced", target, "--check",
	)
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "advanced mode audit", stdout, []string{
		"Advanced-mode migration check (read-only)",
		"Framework: adk",
		"Current mode: managed",
		"Entrypoint: agent:root_agent",
		"tools/",
		"plugins/",
		"skills/",
		"Agent.advanced(...)",
		"spec.framework.mode to advanced",
		"Harnest still owns:",
		"neutral HTTP/SSE/WebSocket",
		"durable tasks/ execution and cron/ schedule discovery",
		"You own in advanced mode:",
		"native graph routing",
		"portable model hooks are not auto-injected",
		"approval and tracing context for explicitly decorated native capabilities",
		"opaque capabilities are not discovered automatically",
		"keep evals as test-only inputs",
		"No files were changed.",
	})
	assertContainsNone(t, "advanced mode audit", stdout, []string{
		"  - lib/",
		"  - subagents/",
		"  - mcp/",
		"  - sandbox/",
	})

	configAfter := mustReadTestFile(t, configPath)
	agentAfter := mustReadTestFile(t, agentPath)
	if string(configAfter) != string(configBefore) || string(agentAfter) != string(agentBefore) {
		t.Fatal("advanced mode check modified config.yaml or agent.py")
	}
}

func mustReadTestFile(t *testing.T, path string) []byte {
	t.Helper()
	contents, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return contents
}

func assertContainsNone(t *testing.T, label, contents string, unexpected []string) {
	t.Helper()
	for _, value := range unexpected {
		if strings.Contains(contents, value) {
			t.Fatalf("%s unexpectedly contains %q:\n%s", label, value, contents)
		}
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
