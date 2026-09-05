package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"harnest.dev/harnest/internal/runtimewheel"
	"harnest.dev/harnest/internal/uvbootstrap"
)

func TestEnvironmentSyncIsIsolatedLockedAndCached(t *testing.T) {
	root := t.TempDir()
	agent := filepath.Join(root, "sync-agent")
	if err := createScaffold(agent, "sync-agent"); err != nil {
		t.Fatal(err)
	}
	resolvedAgent, err := filepath.EvalSymlinks(agent)
	if err != nil {
		t.Fatal(err)
	}
	calls := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	sys := environmentTestSystem(t, root)

	firstOutput := mustSyncAgentEnvironment(t, sys, agent)
	assertContainsAll(t, "sync output", firstOutput, []string{
		"Agent environment ready:", "IDE environment ready:",
	})
	firstCalls := string(mustReadTestFile(t, calls))
	assertContainsAll(t, "uv calls", firstCalls, []string{
		"venv --python 3.12 --managed-python --clear",
		"pip compile --python",
		filepath.Join(resolvedAgent, "pyproject.toml"),
		"pip sync --python",
		"--require-hashes",
	})
	if strings.Contains(firstCalls, procrastinateRequirement) {
		t.Fatalf("task-free agent installed optional task dependencies:\n%s", firstCalls)
	}
	assertFilesExist(t, agent, []string{runtimeRequirementsLockFile, ".harnest/environment.json"})
	lock := string(mustReadTestFile(t, filepath.Join(agent, runtimeRequirementsLockFile)))
	assertContainsAll(t, "committed runtime lock", lock, []string{
		runtimeLockFormatLine,
		runtimeLockDigest,
		runtimeWheelMarker,
		"--hash=sha256:",
	})
	if strings.Contains(lock, root) {
		t.Fatalf("committed runtime lock retained a machine-local path:\n%s", lock)
	}
	idePath := filepath.Join(agent, ".venv")
	firstTarget := mustReadIDEEnvironmentLink(t, idePath)
	mustRecreateCachedIDEEnvironment(t, sys, agent, idePath, calls, firstCalls, firstTarget)

	pyproject := filepath.Join(agent, "pyproject.toml")
	mustWriteEnvironmentFixture(
		t,
		pyproject,
		string(mustReadTestFile(t, pyproject))+"# dependency change\n",
	)
	assertFrozenSyncPreservesIDEEnvironment(t, sys, agent, idePath, calls, firstCalls, firstTarget)
	mustRetargetIDEEnvironment(t, sys, agent, idePath, firstTarget)
}

// mustRecreateCachedIDEEnvironment proves editor setup does not invalidate dependency caching.
func mustRecreateCachedIDEEnvironment(
	t *testing.T,
	sys system,
	agent, idePath, calls, firstCalls, firstTarget string,
) {
	t.Helper()
	if err := os.Remove(idePath); err != nil {
		t.Fatal(err)
	}
	mustSyncAgentEnvironment(t, sys, agent)
	if secondCalls := string(mustReadTestFile(t, calls)); secondCalls != firstCalls {
		t.Fatalf("cached sync invoked uv again:\n%s", secondCalls)
	}
	if cachedTarget := mustReadIDEEnvironmentLink(t, idePath); cachedTarget != firstTarget {
		t.Fatalf("cached sync linked %q, want %q", cachedTarget, firstTarget)
	}
}

// assertFrozenSyncPreservesIDEEnvironment keeps failed resolution from changing editor state.
func assertFrozenSyncPreservesIDEEnvironment(
	t *testing.T,
	sys system,
	agent, idePath, calls, firstCalls, firstTarget string,
) {
	t.Helper()
	_, _, err := executeForTest(t, sys, "env", "sync", agent, "--frozen")
	if err == nil || !strings.Contains(err.Error(), "runtime dependency lock is stale") {
		t.Fatalf("expected stale frozen lock, got %v", err)
	}
	updatedCalls := string(mustReadTestFile(t, calls))
	if strings.Contains(updatedCalls[len(firstCalls):], "pip sync") {
		t.Fatalf("stale frozen lock reached dependency installation:\n%s", updatedCalls)
	}
	if frozenTarget := mustReadIDEEnvironmentLink(t, idePath); frozenTarget != firstTarget {
		t.Fatalf("failed frozen sync changed IDE link from %q to %q", firstTarget, frozenTarget)
	}
}

// mustRetargetIDEEnvironment checks that editor discovery follows a new dependency resolution.
func mustRetargetIDEEnvironment(
	t *testing.T, sys system, agent, idePath, firstTarget string,
) {
	t.Helper()
	mustSyncAgentEnvironment(t, sys, agent)
	if updatedTarget := mustReadIDEEnvironmentLink(t, idePath); updatedTarget == firstTarget {
		t.Fatalf("dependency change did not retarget IDE link %q", updatedTarget)
	}
}

// mustSyncAgentEnvironment keeps command plumbing out of cache-policy assertions.
func mustSyncAgentEnvironment(
	t *testing.T, sys system, agent string, arguments ...string,
) string {
	t.Helper()
	command := append([]string{"env", "sync", agent}, arguments...)
	stdout, _, err := executeForTest(t, sys, command...)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout, "Agent environment ready:") {
		t.Fatalf("unexpected sync output %q", stdout)
	}
	return stdout
}

// mustReadIDEEnvironmentLink verifies that editors see a relative, usable .venv link.
func mustReadIDEEnvironmentLink(t *testing.T, path string) string {
	t.Helper()
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode()&os.ModeSymlink == 0 {
		t.Fatalf("IDE environment %s is not a symlink", path)
	}
	target, err := os.Readlink(path)
	if err != nil {
		t.Fatal(err)
	}
	if filepath.IsAbs(target) || !strings.HasPrefix(
		target,
		filepath.Join(".harnest", "environments")+string(os.PathSeparator),
	) {
		t.Fatalf("IDE environment has unsafe target %q", target)
	}
	if info, err := os.Stat(filepath.Join(path, "bin", "python")); err != nil || !info.Mode().IsRegular() {
		t.Fatalf("IDE Python is unavailable through %s: %v", path, err)
	}
	return target
}

// TestEnvironmentSyncPreservesUserOwnedIDEDirectory keeps local virtual environments intact.
func TestEnvironmentSyncPreservesUserOwnedIDEDirectory(t *testing.T) {
	root, agent := scaffoldIDEEnvironmentTestAgent(t)
	idePath := filepath.Join(agent, ".venv")
	if err := os.Mkdir(idePath, 0o755); err != nil {
		t.Fatal(err)
	}
	mustWriteEnvironmentFixture(t, filepath.Join(idePath, "owned.txt"), "keep\n")
	mustSyncWithUserOwnedIDEEnvironment(t, root, agent)
	if contents := string(mustReadTestFile(t, filepath.Join(idePath, "owned.txt"))); contents != "keep\n" {
		t.Fatalf("user-owned IDE environment changed: %q", contents)
	}
}

// TestEnvironmentSyncPreservesExternalIDEEnvironmentLink avoids claiming arbitrary links.
func TestEnvironmentSyncPreservesExternalIDEEnvironmentLink(t *testing.T) {
	root, agent := scaffoldIDEEnvironmentTestAgent(t)
	external := filepath.Join(root, "external-environment")
	if err := os.Mkdir(external, 0o755); err != nil {
		t.Fatal(err)
	}
	idePath := filepath.Join(agent, ".venv")
	if err := os.Symlink(external, idePath); err != nil {
		t.Fatal(err)
	}
	mustSyncWithUserOwnedIDEEnvironment(t, root, agent)
	if target, err := os.Readlink(idePath); err != nil || target != external {
		t.Fatalf("user-owned IDE link changed to %q: %v", target, err)
	}
}

// scaffoldIDEEnvironmentTestAgent creates an isolated project for link ownership tests.
func scaffoldIDEEnvironmentTestAgent(t *testing.T) (string, string) {
	t.Helper()
	root := t.TempDir()
	agent := filepath.Join(root, "owned-agent")
	if err := createScaffold(agent, "owned-agent"); err != nil {
		t.Fatal(err)
	}
	return root, agent
}

// mustSyncWithUserOwnedIDEEnvironment verifies the non-fatal fallback contract.
func mustSyncWithUserOwnedIDEEnvironment(t *testing.T, root, agent string) {
	t.Helper()
	t.Setenv("HARNEST_ENV_TEST_CALLS", filepath.Join(root, "calls.txt"))
	stdout, stderr, err := executeForTest(
		t, environmentTestSystem(t, root), "env", "sync", agent,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout, "Agent environment ready:") ||
		strings.Contains(stdout, "IDE environment ready:") {
		t.Fatalf("unexpected sync output %q", stdout)
	}
	assertContainsAll(t, "IDE warning", stderr, []string{
		"IDE environment unchanged:",
		"is not managed by Harnest",
		"select ",
		" manually",
	})
}

func TestFrozenEnvironmentSyncRejectsMissingRuntimeLockBeforeUV(t *testing.T) {
	root := t.TempDir()
	agent := filepath.Join(root, "missing-lock-agent")
	if err := createScaffold(agent, "missing-lock-agent"); err != nil {
		t.Fatal(err)
	}
	calls := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	_, _, err := executeForTest(
		t, environmentTestSystem(t, root), "env", "sync", agent, "--frozen",
	)
	if err == nil || !strings.Contains(err.Error(), "frozen runtime dependency lock is unavailable") {
		t.Fatalf("expected missing frozen lock error, got %v", err)
	}
	if _, statErr := os.Stat(calls); !os.IsNotExist(statErr) {
		t.Fatalf("uv ran before frozen lock validation: %v", statErr)
	}
}

func TestRuntimeLockNormalizesMachineLocalWheelAndProjectPaths(t *testing.T) {
	root := t.TempDir()
	agent := filepath.Join(root, "portable-lock-agent")
	if err := createScaffold(agent, "portable-lock-agent"); err != nil {
		t.Fatal(err)
	}
	bundle, err := loadAgentBundle(agent)
	if err != nil {
		t.Fatal(err)
	}
	plan, err := inspectRuntimeDependencyPlan(bundle)
	if err != nil {
		t.Fatal(err)
	}
	wheelPath := filepath.Join(root, "release", "harnest-test.whl")
	candidate := filepath.Join(agent, "candidate.lock")
	mustWriteEnvironmentFixture(t, candidate, fmt.Sprintf(
		"harnest @ %s --hash=sha256:00\nhelper @ %s/vendor/helper.whl --hash=sha256:11\n",
		runtimeWheelURI(wheelPath), strings.TrimSuffix(runtimeWheelURI(bundle.Directory), "/"),
	))
	document, err := normalizeRuntimeLock(
		bundle, runtimewheel.Artifact{Name: "harnest-test.whl", Contents: []byte("wheel")},
		plan, candidate, wheelPath,
	)
	if err != nil {
		t.Fatal(err)
	}
	text := string(document)
	assertContainsAll(t, "portable runtime lock", text, []string{
		runtimeWheelMarker, runtimeProjectMarker + "/vendor/helper.whl",
	})
	if strings.Contains(text, runtimeWheelURI(wheelPath)) || strings.Contains(text, runtimeWheelURI(bundle.Directory)) {
		t.Fatalf("runtime lock retained machine-local paths:\n%s", text)
	}
}

func TestEnvironmentSyncResolvesPluginAndTaskDependenciesTogether(t *testing.T) {
	root := t.TempDir()
	agent := filepath.Join(root, "joint-agent")
	if err := createScaffold(agent, "joint-agent"); err != nil {
		t.Fatal(err)
	}
	resolvedAgent, err := filepath.EvalSymlinks(agent)
	if err != nil {
		t.Fatal(err)
	}
	plugin := filepath.Join(agent, "plugins", "clock")
	if err := os.MkdirAll(plugin, 0o755); err != nil {
		t.Fatal(err)
	}
	mustWriteEnvironmentFixture(t, filepath.Join(plugin, "plugin.yaml"), "kind: RuntimePlugin\n")
	mustWriteEnvironmentFixture(t, filepath.Join(plugin, "pyproject.toml"), `[project]
name = "clock"
version = "1.0.0"
dependencies = ["httpx>=0.28,<1"]
`)
	tasks := filepath.Join(agent, "tasks")
	if err := os.MkdirAll(tasks, 0o755); err != nil {
		t.Fatal(err)
	}
	mustWriteEnvironmentFixture(t, filepath.Join(tasks, "notify.py"), "# compiler validates @task after sync\n")
	calls := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)

	if _, _, err := executeForTest(t, environmentTestSystem(t, root), "env", "sync", agent); err != nil {
		t.Fatal(err)
	}
	contents := string(mustReadTestFile(t, calls))
	assertContainsAll(t, "joint dependency calls", contents, []string{
		"pip compile --python",
		filepath.Join(resolvedAgent, "plugins", "clock", "pyproject.toml"),
		procrastinateRequirement,
		"pip sync --python",
		"--require-hashes",
	})
	lock := string(mustReadTestFile(t, filepath.Join(agent, runtimeRequirementsLockFile)))
	if !strings.Contains(lock, "resolved-runtime-dependencies") {
		t.Fatalf("unexpected generated runtime lock %q", lock)
	}
}

func mustWriteEnvironmentFixture(t *testing.T, path, contents string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(contents), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestCompileUsesSynchronizedAgentPython(t *testing.T) {
	root := t.TempDir()
	agent := filepath.Join(root, "compile-agent")
	if err := createScaffold(agent, "compile-agent"); err != nil {
		t.Fatal(err)
	}
	resolvedAgent, err := filepath.EvalSymlinks(agent)
	if err != nil {
		t.Fatal(err)
	}
	calls := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	sys := environmentTestSystem(t, root)
	output := filepath.Join(root, "artifact")

	if _, _, err := executeForTest(
		t, sys, "compile", agent, "--output", output,
	); err != nil {
		t.Fatal(err)
	}

	contents := string(mustReadTestFile(t, calls))
	assertContainsAll(t, "environment compile calls", contents, []string{
		"venv --python 3.12",
		filepath.Join(resolvedAgent, "pyproject.toml"),
		"pip sync --python",
		"PYTHON -m harnest.cli compile " + resolvedAgent,
	})
}

func environmentTestSystem(t *testing.T, root string) system {
	t.Helper()
	script := `#!/bin/sh
printf 'UV %s\n' "$*" >> "$HARNEST_ENV_TEST_CALLS"
if [ "$1" = "venv" ]; then
	for directory in "$@"; do :; done
	mkdir -p "$directory/bin"
	printf '#!/bin/sh\nprintf '\''PYTHON %%s\\n'\'' "$*" >> "$HARNEST_ENV_TEST_CALLS"\n' > "$directory/bin/python"
	chmod +x "$directory/bin/python"
fi
if [ "$1" = "pip" ] && [ "$2" = "compile" ]; then
	input=""
	while [ "$#" -gt 0 ]; do
		if [ "$1" = "--output-file" ]; then output="$2"; input="$3"; shift 2; continue; fi
		shift
	done
	wheel_uri=$(sed -n 's/^harnest\[[^]]*\] @ //p' "$input")
	printf 'INPUT ' >> "$HARNEST_ENV_TEST_CALLS"
	tr '\n' ' ' < "$input" >> "$HARNEST_ENV_TEST_CALLS"
	printf '\n' >> "$HARNEST_ENV_TEST_CALLS"
	printf 'harnest @ %s \\\n    --hash=sha256:0000000000000000000000000000000000000000000000000000000000000000\nresolved-runtime-dependencies\n' "$wheel_uri" > "$output"
fi
`
	sys := defaultSystem()
	sys.userHomeDir = func() (string, error) { return root, nil }
	sys.commandContext = exec.CommandContext
	sys.embeddedWheel = func(string) (runtimewheel.Artifact, error) {
		return runtimewheel.Artifact{Name: "harnest-test.whl", Contents: []byte("wheel")}, nil
	}
	sys.embeddedUV = func() (uvbootstrap.Artifact, error) {
		return uvbootstrap.Artifact{Name: "uv", Contents: []byte(script)}, nil
	}
	return sys
}
