package main

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// TestEnvironmentSyncSupportsPackDirectory verifies `harnest env sync` attaches Harnest, frameworks, pack packages, and extensions for a pack.
func TestEnvironmentSyncSupportsPackDirectory(t *testing.T) {
	root := t.TempDir()
	packDir := filepath.Join(root, "company-pack")
	writePackFixtureFile(t, filepath.Join(packDir, "pack.yaml"), `apiVersion: harnest.dev/pack/v1
name: company
version: 0.1.0
title: Company Pack
packages:
  - httpx>=0.28
  - packages/company-sdk
  - wheels/custom-1.0.0-py3-none-any.whl
extensions:
  - extensions/company_adk
`)
	writePackFixtureFile(t, filepath.Join(packDir, "packages", "company-sdk", "pyproject.toml"), `[project]
name = "company-sdk"
version = "0.1.0"
`)
	writePackFixtureFile(t, filepath.Join(packDir, "wheels", "custom-1.0.0-py3-none-any.whl"), "fake-wheel-bytes")
	writePackFixtureFile(t, filepath.Join(packDir, "extensions", "company_adk", "extension.yaml"), `apiVersion: harnest.dev/v1alpha1
kind: Extension
metadata:
  name: company_adk
  version: 0.1.0
runtime:
  entrypoint: extension:extension
capabilities:
  - framework.extension
`)
	writePackFixtureFile(t, filepath.Join(packDir, "extensions", "company_adk", "extension.py"), `from harnest.extensions import Extension

class CompanyAdkExtension(Extension):
    pass

extension = CompanyAdkExtension()
`)
	writePackFixtureFile(t, filepath.Join(packDir, "extensions", "company_adk", "pyproject.toml"), `[project]
name = "harnest-extension-company-adk"
version = "0.1.0"
dependencies = ["google-auth>=2.0"]
`)
	// Also include an extension inside extensions/ not yet listed in pack.yaml to verify auto-discovery.
	writePackFixtureFile(t, filepath.Join(packDir, "extensions", "extra_ext", "extension.yaml"), `apiVersion: harnest.dev/v1alpha1
kind: Extension
metadata:
  name: extra_ext
  version: 0.1.0
runtime:
  entrypoint: extension:extension
capabilities: []
`)
	writePackFixtureFile(t, filepath.Join(packDir, "extensions", "extra_ext", "extension.py"), `from harnest.extensions import Extension

class ExtraExtExtension(Extension):
    pass

extension = ExtraExtExtension()
`)
	writePackFixtureFile(t, filepath.Join(packDir, "extensions", "extra_ext", "pyproject.toml"), `[project]
name = "harnest-extension-extra-ext"
version = "0.1.0"
dependencies = ["pydantic-settings>=2.0"]
`)

	resolvedPack, err := filepath.EvalSymlinks(packDir)
	if err != nil {
		t.Fatal(err)
	}
	calls := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	sys := environmentTestSystem(t, root)

	stdout, _, err := executeForTest(t, sys, "env", "sync", packDir)
	if err != nil {
		t.Fatalf("pack env sync failed: %v", err)
	}
	assertContainsAll(t, "pack sync output", stdout, []string{
		"Pack environment ready:",
		"IDE environment ready:",
		"VS Code-compatible editor settings:",
	})

	firstCalls := string(mustReadTestFile(t, calls))
	assertContainsAll(t, "pack uv calls", firstCalls, []string{
		"venv --python >=3.11,<3.15 --python-preference system --clear",
		"pip install --python",
		"harnest[adk,adk-mcp,langgraph,langgraph-mcp,test] @ file://",
		"httpx>=0.28",
		"-e " + filepath.Join(resolvedPack, "packages", "company-sdk"),
		filepath.Join(resolvedPack, "wheels", "custom-1.0.0-py3-none-any.whl"),
		"google-auth>=2.0",
		"pydantic-settings>=2.0",
		"pip check --python",
	})
	assertFilesExist(t, packDir, []string{
		".harnest/authoring-environment.json",
		".vscode/settings.json",
	})
	assertAuthoringIDEAndVSCodePointers(t, packDir)

	// Re-running without changes must hit the fingerprint cache without invoking uv again.
	if _, _, err := executeForTest(t, sys, "env", "sync", packDir); err != nil {
		t.Fatalf("cached pack env sync failed: %v", err)
	}
	secondCalls := string(mustReadTestFile(t, calls))
	if strings.Contains(secondCalls[len(firstCalls):], "UV ") {
		t.Fatalf("cached pack env sync unexpectedly invoked uv:\n%s", secondCalls[len(firstCalls):])
	}

	// Changing an extension pyproject.toml must invalidate the fingerprint and re-sync.
	writePackFixtureFile(t, filepath.Join(packDir, "extensions", "company_adk", "pyproject.toml"), `[project]
name = "harnest-extension-company-adk"
version = "0.1.0"
dependencies = ["google-auth>=2.0", "tenacity>=9.0"]
`)
	if _, _, err := executeForTest(t, sys, "env", "sync", packDir); err != nil {
		t.Fatalf("updated pack env sync failed: %v", err)
	}
	thirdCalls := string(mustReadTestFile(t, calls))
	if !strings.Contains(thirdCalls[len(secondCalls):], "tenacity>=9.0") {
		t.Fatalf("expected updated extension dependency in third sync:\n%s", thirdCalls[len(secondCalls):])
	}
}

// TestEnvironmentSyncSupportsStandaloneExtensionAndInstallSkipsAuthoringState verifies standalone extension sync and subsequent agent installation.
func TestEnvironmentSyncSupportsStandaloneExtensionAndInstallSkipsAuthoringState(t *testing.T) {
	root := t.TempDir()
	extDir := filepath.Join(root, "company_adk")
	if _, _, err := executeForTest(
		t, defaultSystem(), "extensions", "init", "company_adk", "--output", extDir, "--capability", "framework.extension",
	); err != nil {
		t.Fatalf("extensions init --output failed: %v", err)
	}
	writePackFixtureFile(t, filepath.Join(extDir, "pyproject.toml"), `[project]
name = "harnest-extension-company-adk"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["google-auth>=2.0"]

[tool.uv]
package = false
`)
	calls := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	sys := environmentTestSystem(t, root)

	stdout, _, err := executeForTest(t, sys, "env", "sync", extDir, "--framework", "adk")
	if err != nil {
		t.Fatalf("standalone extension env sync failed: %v", err)
	}
	assertContainsAll(t, "extension sync output", stdout, []string{
		"Extension environment ready:",
		"IDE environment ready:",
		"VS Code-compatible editor settings:",
	})
	uvLog := string(mustReadTestFile(t, calls))
	assertContainsAll(t, "extension uv calls", uvLog, []string{
		"harnest[adk,adk-mcp,test] @ file://",
		"google-auth>=2.0",
	})
	if strings.Contains(uvLog, "langgraph") {
		t.Fatalf("--framework adk unexpectedly included langgraph:\n%s", uvLog)
	}
	assertAuthoringIDEAndVSCodePointers(t, extDir)

	// Installing the synced standalone extension into an agent must ignore .harnest, .venv, and .vscode cleanly.
	agentDir := filepath.Join(root, "target-agent")
	if err := createScaffold(agentDir, "target-agent"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := executeForTest(t, defaultSystem(), "extensions", "install", extDir, "--project", agentDir); err != nil {
		t.Fatalf("extensions install failed after env sync: %v", err)
	}
	installed := filepath.Join(agentDir, "extensions", "company_adk")
	for _, skipped := range []string{".harnest", ".venv", ".vscode"} {
		if _, err := os.Lstat(filepath.Join(installed, skipped)); !os.IsNotExist(err) {
			t.Fatalf("installed extension unexpectedly copied authoring state %s", skipped)
		}
	}
}

// TestEnvironmentSyncAuthoringValidationGuards verifies policy enforcement and flag restrictions on packs, extensions, and agents.
func TestEnvironmentSyncAuthoringValidationGuards(t *testing.T) {
	root := t.TempDir()
	packDir := filepath.Join(root, "policy-pack")
	writePackFixtureFile(t, filepath.Join(packDir, "pack.yaml"), `apiVersion: harnest.dev/pack/v1
name: policy
version: 0.1.0
title: Policy Pack
packages:
  - google-adk>=2.8
`)
	sys := environmentTestSystem(t, root)
	// Pack packages declaring compiler-owned frameworks must be rejected.
	if _, _, err := executeForTest(t, sys, "env", "sync", packDir); err == nil || !strings.Contains(err.Error(), "compiler-owned") {
		t.Fatalf("expected compiler-owned rejection for pack.yaml, got: %v", err)
	}

	// Agent-only flags (--frozen and --profile) must be rejected on pack directories.
	writePackFixtureFile(t, filepath.Join(packDir, "pack.yaml"), `apiVersion: harnest.dev/pack/v1
name: policy
version: 0.1.0
title: Policy Pack
`)
	if _, _, err := executeForTest(t, sys, "env", "sync", packDir, "--frozen"); err == nil || !strings.Contains(err.Error(), "--frozen") {
		t.Fatalf("expected --frozen error on pack, got: %v", err)
	}
	if _, _, err := executeForTest(t, sys, "env", "sync", packDir, "--profile", "development"); err == nil || !strings.Contains(err.Error(), "--profile") {
		t.Fatalf("expected --profile error on pack, got: %v", err)
	}
	if _, _, err := executeForTest(t, sys, "env", "sync", packDir, "--framework", "unknown"); err == nil || !strings.Contains(err.Error(), "--framework") {
		t.Fatalf("expected --framework error on pack, got: %v", err)
	}

	// --framework must be rejected on agent projects because config.yaml owns their framework.
	agentDir := filepath.Join(root, "flag-agent")
	if err := createScaffold(agentDir, "flag-agent"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := executeForTest(t, sys, "env", "sync", agentDir, "--framework", "adk"); err == nil || !strings.Contains(err.Error(), "--framework") {
		t.Fatalf("expected --framework error on agent, got: %v", err)
	}
}

// TestEnvironmentSyncAuthoringPinsWorkingDirectoryFromUnlinkedCWD verifies pack env sync never fails when invoked from an unlinked CWD.
func TestEnvironmentSyncAuthoringPinsWorkingDirectoryFromUnlinkedCWD(t *testing.T) {
	root := t.TempDir()
	packDir := filepath.Join(root, "cwd-pack")
	writePackFixtureFile(t, filepath.Join(packDir, "pack.yaml"), `apiVersion: harnest.dev/pack/v1
name: cwd
version: 0.1.0
title: CWD Pack
`)
	resolvedPack, err := filepath.EvalSymlinks(packDir)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("HARNEST_ENV_TEST_CALLS", filepath.Join(root, "calls.txt"))

	var recorded []*exec.Cmd
	sys := environmentTestSystem(t, root)
	sys.commandContext = func(ctx context.Context, name string, args ...string) *exec.Cmd {
		cmd := exec.CommandContext(ctx, name, args...)
		recorded = append(recorded, cmd)
		return cmd
	}

	staleDir := enterUnlinkedWorkingDirectory(t)
	if _, _, err := executeForTest(t, sys, "env", "sync", packDir); err != nil {
		t.Fatalf("pack env sync from unlinked CWD failed: %v", err)
	}
	clearedTarget := filepath.Join(resolvedPack, ".harnest", "environments", authoringEnvironmentSlot)
	for index, cmd := range recorded {
		// Every uv invocation must pin Dir outside both the unlinked caller CWD and the cleared venv target.
		if cmd.Dir == "" || cmd.Dir == staleDir || cmd.Dir == clearedTarget {
			t.Fatalf("command %d (%v) used invalid Dir=%q (stale=%q clearedTarget=%q)", index, cmd.Args, cmd.Dir, staleDir, clearedTarget)
		}
	}
}

// assertAuthoringIDEAndVSCodePointers verifies .venv and .vscode/settings.json target the authoring environment slot.
func assertAuthoringIDEAndVSCodePointers(t *testing.T, projectRoot string) {
	t.Helper()
	expectedTarget := filepath.Join(".harnest", "environments", authoringEnvironmentSlot)
	actualTarget := mustReadIDEEnvironmentLink(t, filepath.Join(projectRoot, ".venv"))
	if actualTarget != expectedTarget {
		t.Fatalf(".venv points to %q, want %q", actualTarget, expectedTarget)
	}
	contents := mustReadTestFile(t, filepath.Join(projectRoot, ".vscode", "settings.json"))
	var settings map[string]string
	if err := json.Unmarshal(contents, &settings); err != nil {
		t.Fatal(err)
	}
	expectedInterpreter := "${workspaceFolder}/" + filepath.ToSlash(runtimePythonPath(expectedTarget))
	if settings[vscodeInterpreterKey] != expectedInterpreter {
		t.Fatalf("VS Code interpreter is %q, want %q", settings[vscodeInterpreterKey], expectedInterpreter)
	}
}
