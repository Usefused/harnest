package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"harnest.dev/harnest/internal/runtimewheel"
)

func TestRuntimeInstallAutoDiscoversSupportedPython(t *testing.T) {
	binDirectory := t.TempDir()
	writeNamedExecutable(t, binDirectory, "python3", `#!/bin/sh
printf '%s\n' '3.9.6'
exit 1
`)
	supported := writeNamedExecutable(t, binDirectory, "python3.11", `#!/bin/sh
printf '%s\n' '3.11.9'
`)
	t.Setenv("PATH", binDirectory)
	app := application{system: defaultSystem()}

	executable, err := app.resolveBootstrapPython(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	if executable != supported {
		t.Fatalf("selected %q, want supported interpreter %q", executable, supported)
	}
}

func TestRuntimeInstallReportsDiscoveredUnsupportedPython(t *testing.T) {
	binDirectory := t.TempDir()
	unsupported := writeNamedExecutable(t, binDirectory, "python3", `#!/bin/sh
printf '%s\n' '3.9.6'
exit 1
`)
	t.Setenv("PATH", binDirectory)
	app := application{system: defaultSystem()}

	_, err := app.resolveBootstrapPython(context.Background(), "")
	if err == nil {
		t.Fatal("unsupported Python unexpectedly passed discovery")
	}
	assertContainsAll(t, "Python discovery error", err.Error(), []string{
		"Python 3.10 or newer was not found",
		"Python 3.9.6 at " + unsupported + " is unsupported",
		"HARNEST_BOOTSTRAP_PYTHON",
	})
}

func TestRuntimeInstallBootstrapsFromEmbeddedWheel(t *testing.T) {
	record := filepath.Join(t.TempDir(), "runtime-calls.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	bootstrap := writeExecutable(t, `#!/bin/sh
printf 'BOOTSTRAP\t%s\n' "$*" >> "$HARNEST_TEST_RECORD"
if [ "$1" = "-c" ]; then exit 0; fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  printf '%s\n' '#!/bin/sh' 'printf "RUNTIME\\t%s\\n" "$*" >> "$HARNEST_TEST_RECORD"' > "$3/bin/python"
  chmod 0755 "$3/bin/python"
fi
`)
	sys := defaultSystem()
	sys.embeddedWheel = func(version string) (runtimewheel.Artifact, error) {
		if version != "test-version" {
			t.Fatalf("wheel requested for version %q", version)
		}
		return runtimewheel.Artifact{
			Name:     "harnest-test-version-py3-none-any.whl",
			Contents: []byte("embedded wheel"),
		}, nil
	}
	runtimeDirectory := filepath.Join(t.TempDir(), "runtime")

	stdout, _, err := executeForTest(
		t,
		sys,
		"runtime", "install",
		"--bootstrap-python", bootstrap,
		"--directory", runtimeDirectory,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout, "Installed embedded Harnest runtime test-version") {
		t.Fatalf("unexpected output %q", stdout)
	}
	calls, err := os.ReadFile(record)
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "runtime install calls", string(calls), []string{
		"BOOTSTRAP\t-c import platform, sys;",
		"BOOTSTRAP\t-m venv " + runtimeDirectory,
		"RUNTIME\t-m pip --disable-pip-version-check install --upgrade ",
		"harnest-test-version-py3-none-any.whl[all]",
	})
	assertStagedWheelRemoved(t, string(calls))
}

func assertStagedWheelRemoved(t *testing.T, calls string) {
	t.Helper()
	for _, field := range strings.Fields(calls) {
		if strings.HasSuffix(field, ".whl[all]") {
			wheel := strings.TrimSuffix(field, "[all]")
			if _, err := os.Stat(wheel); !os.IsNotExist(err) {
				t.Fatalf("staged wheel was not removed: %v", err)
			}
			return
		}
	}
	t.Fatal("pip invocation did not include a staged wheel")
}

func writeNamedExecutable(t *testing.T, directory, name, contents string) string {
	t.Helper()
	path := filepath.Join(directory, name)
	if err := os.WriteFile(path, []byte(contents), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}
