package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestEnvironmentPruningPreservesCurrentAndLeasedRuntimes(t *testing.T) {
	project := t.TempDir()
	current := "1111111111111111"
	leased := "2222222222222222"
	stale := "3333333333333333"
	developmentCurrent := "4444444444444444"
	for _, name := range []string{current, leased, stale, developmentCurrent} {
		writeTestAgentEnvironment(t, project, name)
	}
	state := filepath.Join(project, ".harnest", environmentStateFile)
	if err := writeEnvironmentState(state, environmentState{
		Fingerprint: current,
		Directory:   filepath.ToSlash(filepath.Join("environments", current)),
	}); err != nil {
		t.Fatal(err)
	}
	developmentState := filepath.Join(
		project, ".harnest", developmentEnvironmentProfile.stateFile(),
	)
	if err := writeEnvironmentState(developmentState, environmentState{
		Fingerprint: developmentCurrent,
		Directory:   filepath.ToSlash(filepath.Join("environments", developmentCurrent)),
	}); err != nil {
		t.Fatal(err)
	}
	selection, err := leaseAgentPython(
		project,
		pythonSelection{
			Executable: runtimePythonPath(testAgentEnvironment(project, leased)),
			Source:     "agent environment",
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	overlapping, err := leaseAgentPython(
		project,
		pythonSelection{
			Executable: runtimePythonPath(testAgentEnvironment(project, leased)),
			Source:     "agent environment",
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	waitForArtifactRemoval(t, testAgentEnvironment(project, stale))
	assertTestEnvironmentExists(t, project, current)
	assertTestEnvironmentExists(t, project, leased)
	assertTestEnvironmentExists(t, project, developmentCurrent)

	selection.releaseLease()
	assertTestEnvironmentExists(t, project, leased)
	overlapping.releaseLease()
	waitForArtifactRemoval(t, testAgentEnvironment(project, leased))
	assertTestEnvironmentExists(t, project, current)
	assertTestEnvironmentExists(t, project, developmentCurrent)
}

// writeTestAgentEnvironment creates the interpreter shape recognized as managed.
func writeTestAgentEnvironment(t *testing.T, project, name string) {
	t.Helper()
	environment := testAgentEnvironment(project, name)
	if err := os.MkdirAll(filepath.Join(environment, "bin"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(runtimePythonPath(environment), []byte("python\n"), 0o755); err != nil {
		t.Fatal(err)
	}
}

// testAgentEnvironment returns one cache-owned fingerprint directory.
func testAgentEnvironment(project, name string) string {
	return filepath.Join(project, ".harnest", "environments", name)
}

// assertTestEnvironmentExists verifies pruning retained an owned runtime.
func assertTestEnvironmentExists(t *testing.T, project, name string) {
	t.Helper()
	if _, err := os.Stat(runtimePythonPath(testAgentEnvironment(project, name))); err != nil {
		t.Fatalf("environment %s is unavailable: %v", name, err)
	}
}
