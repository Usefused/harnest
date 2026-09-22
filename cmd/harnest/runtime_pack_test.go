package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"harnest.dev/harnest/internal/agentpack"
)

// packProject creates dependency-only fixtures without executing authored Python.
func packProject(t *testing.T, name, requirements string) string {
	t.Helper()
	root := filepath.Join(t.TempDir(), name)
	if err := createScaffold(root, name); err != nil {
		t.Fatal(err)
	}
	project := "[project]\nname = \"" + name + "\"\nversion = \"0.1.0\"\ndependencies = [" + requirements + "]\n"
	mustWriteEnvironmentFixture(t, filepath.Join(root, "pyproject.toml"), project)
	return root
}

// TestSharedRuntimePlanDeduplicatesOwners includes all declarations while sharing equivalent input identities.
func TestSharedRuntimePlanDeduplicatesOwners(t *testing.T) {
	first := packProject(t, "sales", `"httpx>=0.28"`)
	second := packProject(t, "support", `"httpx>=0.28"`)
	plan, err := sharedRuntimePlan([]string{first, second})
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.Projects) != 2 || len(plan.Inputs) != 1 || len(plan.Requirements) != 1 {
		t.Fatalf("bad shared closure: %#v", plan)
	}
	bundle, err := loadAgentBundle(first)
	if err != nil {
		t.Fatal(err)
	}
	manifest := agentpack.Manifest{Inputs: plan.Inputs}
	if err = validatePackAgent(bundle, manifest); err != nil {
		t.Fatal(err)
	}
	mustWriteEnvironmentFixture(t, filepath.Join(first, "pyproject.toml"), "[project]\nname='sales'\nversion='0.1.0'\ndependencies=['httpx==0.27.0']\n")
	if err = validatePackAgent(bundle, manifest); err == nil {
		t.Fatal("changed dependencies reused old runtime")
	}
}

// TestSharedRuntimePlanRejectsPythonMismatch avoids creating a misleading cross-ABI pack.
func TestSharedRuntimePlanRejectsPythonMismatch(t *testing.T) {
	first := packProject(t, "sales", "")
	second := packProject(t, "support", "")
	config := filepath.Join(second, "config.yaml")
	data, err := os.ReadFile(config)
	if err != nil {
		t.Fatal(err)
	}
	data = []byte(strings.ReplaceAll(string(data), "3.12", "3.11"))
	if err = os.WriteFile(config, data, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = sharedRuntimePlan([]string{first, second}); err == nil {
		t.Fatal("incompatible Python versions accepted")
	}
}

// TestRuntimePackTracksCommittedLockChanges rejects an attachment after production pins change.
func TestRuntimePackTracksCommittedLockChanges(t *testing.T) {
	root := packProject(t, "sales", "")
	plan, err := sharedRuntimePlan([]string{root})
	if err != nil {
		t.Fatal(err)
	}
	mustWriteEnvironmentFixture(t, filepath.Join(root, runtimeRequirementsLockFile), "changed production pins")
	bundle, err := loadAgentBundle(root)
	if err != nil {
		t.Fatal(err)
	}
	if err = validatePackAgent(bundle, agentpack.Manifest{Inputs: plan.Inputs}); err == nil {
		t.Fatal("modified lock reused old runtime")
	}
}

// TestRuntimeBuildRefusesExistingOutput preserves immutable attachments during accidental rebuilds.
func TestRuntimeBuildRefusesExistingOutput(t *testing.T) {
	if err := agentpack.SupportedHost(); err != nil {
		t.Skip(err)
	}
	root := t.TempDir()
	marker := filepath.Join(root, "keep")
	mustWriteEnvironmentFixture(t, marker, "unchanged")
	_, _, err := executeForTest(t, defaultSystem(), "runtime", "build", "--agent", "unused", "--output", root)
	if err == nil || !strings.Contains(err.Error(), "must not exist") {
		t.Fatalf("unexpected error: %v", err)
	}
	if string(mustReadTestFile(t, marker)) != "unchanged" {
		t.Fatal("existing runtime overwritten")
	}
}

// TestPortableConsoleScriptsRelocateQuotedInterpreterPaths preserves console tools after a runtime moves.
func TestPortableConsoleScriptsRelocateQuotedInterpreterPaths(t *testing.T) {
	python := "/build directory/python3"
	sources := []string{"#!" + python + "\nimport tool\n", "#!/bin/sh\n'''exec' '/build directory/python3' \"$0\" \"$@\"\n' '''\nimport tool\n"}
	for _, source := range sources {
		result := string(portableConsoleScript([]byte(source), python))
		if result != "#!/usr/bin/env python3\nimport tool\n" {
			t.Fatalf("console script retained build path: %q", result)
		}
	}
	shell := "#!/bin/sh\necho preserve-shell-tool\n"
	if string(portableConsoleScript([]byte(shell), python)) != shell {
		t.Fatal("ordinary shell tool rewritten")
	}
}
