package main

import (
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

	mustSyncAgentEnvironment(t, sys, agent)
	firstCalls := string(mustReadTestFile(t, calls))
	assertContainsAll(t, "uv calls", firstCalls, []string{
		"sync --project " + resolvedAgent,
		"--python 3.12 --managed-python",
		"pip install --python",
		"harnest-test.whl[adk]",
	})
	if strings.Contains(firstCalls, "pip compile") || strings.Contains(firstCalls, procrastinateRequirement) {
		t.Fatalf("task-free agent installed optional runtime dependencies:\n%s", firstCalls)
	}
	assertFilesExist(t, agent, []string{"uv.lock", ".harnest/environment.json"})

	mustSyncAgentEnvironment(t, sys, agent)
	if secondCalls := string(mustReadTestFile(t, calls)); secondCalls != firstCalls {
		t.Fatalf("cached sync invoked uv again:\n%s", secondCalls)
	}

	pyproject := filepath.Join(agent, "pyproject.toml")
	mustWriteEnvironmentFixture(
		t,
		pyproject,
		string(mustReadTestFile(t, pyproject))+"# dependency change\n",
	)
	mustSyncAgentEnvironment(t, sys, agent, "--frozen")
	updatedCalls := string(mustReadTestFile(t, calls))
	if !strings.Contains(updatedCalls[len(firstCalls):], "--frozen") {
		t.Fatalf("frozen resync did not reach uv:\n%s", updatedCalls)
	}
}

// mustSyncAgentEnvironment keeps command plumbing out of cache-policy assertions.
func mustSyncAgentEnvironment(
	t *testing.T, sys system, agent string, arguments ...string,
) {
	t.Helper()
	command := append([]string{"env", "sync", agent}, arguments...)
	stdout, _, err := executeForTest(t, sys, command...)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout, "Agent environment ready:") {
		t.Fatalf("unexpected sync output %q", stdout)
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
		filepath.Join(resolvedAgent, ".harnest", runtimeTaskInputFile),
		"pip install --python",
		"--require-hashes -r " + filepath.Join(resolvedAgent, ".harnest", runtimeRequirementsLockFile),
	})
	lock := string(mustReadTestFile(t, filepath.Join(agent, ".harnest", runtimeRequirementsLockFile)))
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
		"sync --project " + resolvedAgent,
		"pip install --python",
		"PYTHON -m harnest.cli compile " + resolvedAgent,
	})
}

func environmentTestSystem(t *testing.T, root string) system {
	t.Helper()
	script := `#!/bin/sh
printf 'UV %s\n' "$*" >> "$HARNEST_ENV_TEST_CALLS"
if [ "$1" = "sync" ]; then
  shift
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--project" ]; then project="$2"; shift 2; continue; fi
    shift
  done
  mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
  printf '#!/bin/sh\nprintf '\''PYTHON %%s\\n'\'' "$*" >> "$HARNEST_ENV_TEST_CALLS"\n' > "$UV_PROJECT_ENVIRONMENT/bin/python"
  chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python"
  printf 'version = 1\n' > "$project/uv.lock"
fi
if [ "$1" = "pip" ] && [ "$2" = "compile" ]; then
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--output-file" ]; then output="$2"; shift 2; continue; fi
    shift
  done
  printf 'resolved-runtime-dependencies\n' > "$output"
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
