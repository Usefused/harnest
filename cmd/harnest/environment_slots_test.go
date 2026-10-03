package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/internal/uvbootstrap"
)

// TestEnvironmentSlotsRemainBounded exercises profile changes, cache invalidation, and dependency edits.
func TestEnvironmentSlotsRemainBounded(t *testing.T) {
	root, agent := scaffoldIDEEnvironmentTestAgent(t)
	calls := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	sys := environmentTestSystem(t, root)
	for _, profile := range []string{"runtime", "compile", "development", "eval", "runtime", "development"} {
		_, _, err := executeForTest(t, sys, "env", "sync", agent, "--profile", profile)
		if err != nil {
			t.Fatal(err)
		}
		assertVSCodeProfileInterpreter(t, agent, environmentProfile(profile))
		before := string(mustReadTestFile(t, calls))
		if _, _, err := executeForTest(t, sys, "env", "sync", agent, "--profile", profile); err != nil {
			t.Fatal(err)
		}
		if after := string(mustReadTestFile(t, calls)); after != before {
			t.Fatal("unchanged profile was not cached")
		}
	}
	if count := strings.Count(string(mustReadTestFile(t, calls)), "UV venv"); count != 6 {
		t.Fatalf("profile switches should install their own dependency set; got %d syncs", count)
	}
	mustChangeEnvironmentDependencyInput(t, agent)
	mustSyncAgentEnvironment(t, sys, agent)
	assertEnvironmentSlots(t, agent, "agent", "eval")
}

// TestEnvironmentSlotsMigrateLegacyCaches also retires stale profile pointers and the old IDE target.
func TestEnvironmentSlotsMigrateLegacyCaches(t *testing.T) {
	root, agent := scaffoldIDEEnvironmentTestAgent(t)
	t.Setenv("HARNEST_ENV_TEST_CALLS", filepath.Join(root, "calls.txt"))
	names := []string{"1111111111111111", "2222222222222222", "3333333333333333", "4444444444444444"}
	states := []string{environmentStateFile, "environment-compile.json", "environment-development.json", "environment-eval.json"}
	for i, name := range names {
		writeTestAgentEnvironment(t, agent, name)
		if err := writeEnvironmentState(filepath.Join(agent, ".harnest", states[i]), environmentState{Directory: "environments/" + name}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := syncIDEEnvironmentLink(agent, runtimePythonPath(testAgentEnvironment(agent, names[0]))); err != nil {
		t.Fatal(err)
	}
	assertLegacyEnvironmentLeaseBlocksMigration(t, environmentTestSystem(t, root), agent, names[0])
	mustSyncAgentEnvironment(t, environmentTestSystem(t, root), agent)
	assertEnvironmentSlots(t, agent, "agent")
	if target := mustReadIDEEnvironmentLink(t, filepath.Join(agent, ".venv")); filepath.Base(target) != "agent" {
		t.Fatalf("legacy editor link remains: %s", target)
	}
	for _, name := range states[1:] {
		if _, err := os.Stat(filepath.Join(agent, ".harnest", name)); !os.IsNotExist(err) {
			t.Fatalf("legacy state remains: %s: %v", name, err)
		}
	}
}

// TestEnvironmentSlotsProtectRunningCommands rejects in-place replacement but allows independent evals.
func TestEnvironmentSlotsProtectRunningCommands(t *testing.T) {
	root, agent := scaffoldIDEEnvironmentTestAgent(t)
	t.Setenv("HARNEST_ENV_TEST_CALLS", filepath.Join(root, "calls.txt"))
	sys := environmentTestSystem(t, root)
	app := &application{system: sys, version: "test-version"}
	command := &cobra.Command{}
	command.SetContext(context.Background())
	bundle, err := loadAgentBundle(agent)
	if err != nil {
		t.Fatal(err)
	}
	python, err := app.agentPython(command, bundle, runtimeEnvironmentProfile)
	if err != nil {
		t.Fatal(err)
	}
	defer python.releaseLease()
	if _, _, err := executeForTest(t, sys, "env", "sync", agent, "--profile", "eval"); err != nil {
		t.Fatal(err)
	}
	mustChangeEnvironmentDependencyInput(t, agent)
	bundle, err = loadAgentBundle(agent)
	if err != nil {
		t.Fatal(err)
	}
	// Source builds must not bypass the lease through their system-Python fallback.
	if _, err := app.agentPython(command, bundle, runtimeEnvironmentProfile); err == nil || !strings.Contains(err.Error(), "is in use") {
		t.Fatalf("expected live environment rejection: %v", err)
	}
	python.releaseLease()
	mustSyncAgentEnvironment(t, sys, agent)
	assertEnvironmentSlots(t, agent, "agent", "eval")
}

// mustChangeEnvironmentDependencyInput invalidates the lock fingerprint without introducing network dependencies.
func mustChangeEnvironmentDependencyInput(t *testing.T, agent string) {
	t.Helper()
	path := filepath.Join(agent, "pyproject.toml")
	contents := string(mustReadTestFile(t, path))
	mustWriteEnvironmentFixture(t, path, contents+"\n# dependency input changed\n")
}

// assertEnvironmentSlots counts physical environments, including unexpected legacy copies.
func assertEnvironmentSlots(t *testing.T, agent string, names ...string) {
	t.Helper()
	entries, err := os.ReadDir(filepath.Join(agent, ".harnest", "environments"))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != len(names) {
		t.Fatalf("got %d environments, want %v: %v", len(entries), names, entries)
	}
	for _, name := range names {
		assertTestEnvironmentExists(t, agent, name)
	}
}

// TestEnvironmentSlotsRetryFailedSync prevents a partial installation from being accepted as cached.
func TestEnvironmentSlotsRetryFailedSync(t *testing.T) {
	root, agent := scaffoldIDEEnvironmentTestAgent(t)
	calls := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	sys := environmentTestSystem(t, root)
	mustSyncAgentEnvironment(t, sys, agent)
	mustChangeEnvironmentDependencyInput(t, agent)
	failing := sys
	failing.embeddedUV = func() (uvbootstrap.Artifact, error) {
		artifact, err := sys.embeddedUV()
		artifact.Contents = []byte(strings.Replace(string(artifact.Contents), "#!/bin/sh", "#!/bin/sh\nif [ \"$1\" = \"pip\" ] && [ \"$2\" = \"sync\" ]; then exit 42; fi", 1))
		return artifact, err
	}
	if _, _, err := executeForTest(t, failing, "env", "sync", agent); err == nil {
		t.Fatal("expected sync failure")
	}
	if _, err := os.Stat(filepath.Join(agent, ".harnest", environmentStateFile)); !os.IsNotExist(err) {
		t.Fatalf("failed sync left a valid publication: %v", err)
	}
	before := string(mustReadTestFile(t, calls))
	mustSyncAgentEnvironment(t, sys, agent)
	if after := string(mustReadTestFile(t, calls)); after == before {
		t.Fatal("failed installation was accepted as cached")
	}
	assertEnvironmentSlots(t, agent, "agent")
}

// assertLegacyEnvironmentLeaseBlocksMigration preserves interpreters selected by older CLI processes.
func assertLegacyEnvironmentLeaseBlocksMigration(t *testing.T, sys system, agent, name string) {
	t.Helper()
	selection, err := leaseAgentPython(agent, pythonSelection{Executable: runtimePythonPath(testAgentEnvironment(agent, name))})
	if err != nil {
		t.Fatal(err)
	}
	defer selection.releaseLease()
	if _, _, err := executeForTest(t, sys, "env", "sync", agent); err == nil || !strings.Contains(err.Error(), "is in use") {
		t.Fatalf("migration should wait for legacy commands: %v", err)
	}
	assertTestEnvironmentExists(t, agent, name)
	if _, err := os.Stat(testAgentEnvironment(agent, "agent")); !os.IsNotExist(err) {
		t.Fatalf("migration created an extra environment while legacy commands are active: %v", err)
	}
}
