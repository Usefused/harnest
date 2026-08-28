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

	stdout, _, err := executeForTest(t, sys, "env", "sync", agent)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout, "Agent environment ready:") {
		t.Fatalf("unexpected sync output %q", stdout)
	}
	firstCalls := string(mustReadTestFile(t, calls))
	assertContainsAll(t, "uv calls", firstCalls, []string{
		"sync --project " + resolvedAgent,
		"--python 3.12 --managed-python",
		"pip install --python",
		"harnest-test.whl[adk]",
	})
	assertFilesExist(t, agent, []string{"uv.lock", ".harnest/environment.json"})

	if _, _, err := executeForTest(t, sys, "env", "sync", agent); err != nil {
		t.Fatal(err)
	}
	if secondCalls := string(mustReadTestFile(t, calls)); secondCalls != firstCalls {
		t.Fatalf("cached sync invoked uv again:\n%s", secondCalls)
	}

	pyproject := filepath.Join(agent, "pyproject.toml")
	if err := os.WriteFile(
		pyproject,
		append(mustReadTestFile(t, pyproject), []byte("# dependency change\n")...),
		0o644,
	); err != nil {
		t.Fatal(err)
	}
	if _, _, err := executeForTest(t, sys, "env", "sync", agent, "--frozen"); err != nil {
		t.Fatal(err)
	}
	updatedCalls := string(mustReadTestFile(t, calls))
	if !strings.Contains(updatedCalls[len(firstCalls):], "--frozen") {
		t.Fatalf("frozen resync did not reach uv:\n%s", updatedCalls)
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
