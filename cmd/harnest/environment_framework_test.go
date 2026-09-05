package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFrameworkPinRequirement(t *testing.T) {
	pin := []byte("apiVersion: harnest.dev/v1alpha1\nkind: ProjectLock\nprojectSchema: 3\nframework:\n  name: langgraph\n  distribution: langgraph\n  version: 1.2.11\n")
	requirement, err := parseFrameworkRequirement(pin, "langgraph")
	if err != nil || requirement != "langgraph==1.2.11" {
		t.Fatalf("pin: %q %v", requirement, err)
	}
	requirement, err = parseFrameworkRequirement(pin, "adk")
	if err != nil || requirement != "" {
		t.Fatalf("switch: %q %v", requirement, err)
	}
	for _, bad := range []string{
		"framework: {name: langgraph, distribution: other, version: 1.2.11}",
		"framework: {name: langgraph, distribution: langgraph, version: '--index-url=other'}",
		"framework: {name: unknown, distribution: unknown, version: 1.0}",
	} {
		if _, err := parseFrameworkRequirement([]byte(bad), "langgraph"); err == nil {
			t.Fatalf("accepted %s", bad)
		}
	}
}

func TestEnvironmentSyncAppliesFrameworkPinAndRecordsInstalledVersion(t *testing.T) {
	root := t.TempDir()
	agent := filepath.Join(root, "pinned-agent")
	if err := createScaffold(agent, "pinned-agent"); err != nil {
		t.Fatal(err)
	}
	lockPath := filepath.Join(agent, "harnest.lock")
	mustWriteEnvironmentFixture(t, lockPath, string(mustReadTestFile(t, lockPath))+"framework:\n  name: adk\n  distribution: google-adk\n  version: 2.8.0\n")
	calls := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	sys := environmentTestSystem(t, root)
	mustSyncAgentEnvironment(t, sys, agent)
	if !strings.Contains(string(mustReadTestFile(t, calls)), "google-adk==2.8.0") {
		t.Fatal("runtime resolution input omitted the committed framework pin")
	}
	if err := os.Remove(filepath.Join(agent, ".harnest", environmentStateFile)); err != nil {
		t.Fatal(err)
	}
	mustWriteEnvironmentFixture(t, calls, "")
	mustSyncAgentEnvironment(t, sys, agent, "--frozen")
	assertContainsAll(t, "framework sync process calls", string(mustReadTestFile(t, calls)), []string{
		"pip sync --python",
		"pip check --python",
		"-m harnest.project_lock",
		"adk --frozen",
	})
	if strings.Contains(string(mustReadTestFile(t, calls)), "pip compile") {
		t.Fatal("frozen sync unexpectedly resolved dependencies")
	}
}
