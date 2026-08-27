package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"harnest.dev/harnest/internal/runtimewheel"
	"harnest.dev/harnest/internal/uvbootstrap"
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
	})
}

func TestRuntimeInstallFallsBackWhenHostPythonCannotCreateEnvironment(t *testing.T) {
	record := filepath.Join(t.TempDir(), "uv-calls.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	hostPython := writeExecutable(t, `#!/bin/sh
if [ "$1" = "-c" ]; then
  printf '%s\n' '3.11.9'
  exit 0
fi
exit 1
`)
	sys := defaultSystem()
	sys.lookPath = func(candidate string) (string, error) {
		if candidate == "python3.11" {
			return hostPython, nil
		}
		return "", os.ErrNotExist
	}
	sys.embeddedWheel = testEmbeddedWheel(t)
	sys.embeddedUV = func() (uvbootstrap.Artifact, error) {
		return uvbootstrap.Artifact{Name: "uv", Contents: []byte(`#!/bin/sh
printf 'UV\t%s\t%s\t%s\n' "$0" "$UV_PYTHON_INSTALL_DIR" "$*" >> "$HARNEST_TEST_RECORD"
if [ "$1" = "venv" ]; then
  /bin/mkdir -p "$6/bin"
  printf '%s\n' '#!/bin/sh' > "$6/bin/python"
  /bin/chmod 0755 "$6/bin/python"
fi
`)}, nil
	}
	runtimeDirectory := filepath.Join(t.TempDir(), "runtime")

	stdout, _, err := executeForTest(t, sys, "runtime", "install", "--directory", runtimeDirectory)
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "managed Python output", stdout, []string{
		"installing managed Python 3.12 with embedded uv 0.12.6",
		"Installed embedded Harnest runtime test-version",
	})
	calls, err := os.ReadFile(record)
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "embedded uv calls", string(calls), []string{
		managedPythonDirectory(runtimeDirectory),
		"venv --python 3.12 --managed-python --clear " + runtimeDirectory,
		"pip install --python " + runtimePythonPath(runtimeDirectory) + " --upgrade ",
		"harnest-test-version-py3-none-any.whl[all]",
	})
	assertStagedUVRemoved(t, string(calls))
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
	sys.embeddedWheel = testEmbeddedWheel(t)
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

func testEmbeddedWheel(t *testing.T) func(string) (runtimewheel.Artifact, error) {
	t.Helper()
	return func(version string) (runtimewheel.Artifact, error) {
		if version != "test-version" {
			t.Fatalf("wheel requested for version %q", version)
		}
		return runtimewheel.Artifact{
			Name:     "harnest-test-version-py3-none-any.whl",
			Contents: []byte("embedded wheel"),
		}, nil
	}
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

func assertStagedUVRemoved(t *testing.T, calls string) {
	t.Helper()
	for _, line := range strings.Split(calls, "\n") {
		fields := strings.Split(line, "\t")
		if len(fields) > 1 && fields[0] == "UV" {
			if _, err := os.Stat(fields[1]); !os.IsNotExist(err) {
				t.Fatalf("staged uv was not removed: %v", err)
			}
			return
		}
	}
	t.Fatal("uv invocation was not recorded")
}

func writeNamedExecutable(t *testing.T, directory, name, contents string) string {
	t.Helper()
	path := filepath.Join(directory, name)
	if err := os.WriteFile(path, []byte(contents), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}
