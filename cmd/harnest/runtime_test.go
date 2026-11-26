package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"harnest.dev/harnest/internal/runtimewheel"
	"harnest.dev/harnest/internal/uvbootstrap"
)

// TestBootstrapPythonMinimum executes the actual probe at the supported version boundary.
func TestBootstrapPythonMinimum(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("Python is unavailable for bootstrap probe integration")
	}
	for _, minor := range []int{10, 11, 14} {
		t.Run(fmt.Sprintf("3.%d", minor), func(t *testing.T) {
			sys := defaultSystem()
			sys.commandContext = func(ctx context.Context, _ string, args ...string) *exec.Cmd {
				// Simulate only the version boundary; execute the real validation program.
				program := fmt.Sprintf("import sys; sys.version_info = (3, %d); ", minor) + args[1]
				return exec.CommandContext(ctx, python, "-c", program)
			}
			app := application{system: sys}
			err := app.validateBootstrapPython(context.Background(), python)
			// Python 3.11 and newer satisfy the minimum runtime floor.
			if (err == nil) != (minor >= 11) {
				t.Fatalf("Python 3.%d: validation error = %v", minor, err)
			}
		})
	}
}

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
		"Python 3.11 or newer was not found",
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
		"installing managed Python 3.14 with embedded uv 0.12.6",
		"Installed embedded Harnest runtime test-version",
	})
	calls, err := os.ReadFile(record)
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "embedded uv calls", string(calls), []string{
		managedPythonDirectory(runtimeDirectory),
		"venv --python 3.14 --managed-python --clear " + runtimeDirectory,
		"pip install --python " + runtimePythonPath(runtimeDirectory) + " --upgrade ",
		"harnest-test-version-py3-none-any.whl",
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
	// Exercise the stdlib venv + pip fallback when neither system uv nor embedded uv is available.
	sys.lookPath = func(name string) (string, error) {
		if name == "uv" {
			return "", os.ErrNotExist
		}
		return exec.LookPath(name)
	}
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
		"harnest-test-version-py3-none-any.whl",
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
		if strings.HasSuffix(field, ".whl") {
			wheel := field
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

// TestInstallRuntimeWithManagedPythonPinsWorkingDirectory verifies managed uv install avoids caller CWD, --clear target, and missing parents.
func TestInstallRuntimeWithManagedPythonPinsWorkingDirectory(t *testing.T) {
	cases := []struct {
		name         string
		parentExists bool
	}{
		{name: "existing_parent", parentExists: true},
		{name: "missing_parent_falls_back_to_staged_uv_dir", parentExists: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			record := filepath.Join(root, "runtime-uv-pwd.txt")
			t.Setenv("HARNEST_UV_PWD_RECORD", record)
			runtimeDirectory := filepath.Join(root, "runtime")
			// Simulate a first-time install where ~/.harnest does not exist yet on disk.
			if !tc.parentExists {
				runtimeDirectory = filepath.Join(root, "uncreated-home", "runtime")
			}

			var recorded []*exec.Cmd
			sys := defaultSystem()
			sys.lookPath = func(string) (string, error) { return "", os.ErrNotExist }
			sys.embeddedWheel = testEmbeddedWheel(t)
			sys.embeddedUV = func() (uvbootstrap.Artifact, error) {
				return uvbootstrap.Artifact{Name: "uv", Contents: clearSimulatingUVScript()}, nil
			}
			sys.commandContext = func(ctx context.Context, name string, args ...string) *exec.Cmd {
				cmd := exec.CommandContext(ctx, name, args...)
				recorded = append(recorded, cmd)
				return cmd
			}

			staleDir := enterUnlinkedWorkingDirectory(t)
			_, _, err := executeForTest(t, sys, "runtime", "install", "--directory", runtimeDirectory)
			// Managed Python bootstrap must succeed from an unlinked CWD regardless of whether parent exists yet.
			if err != nil {
				t.Fatalf("runtime install failed: %v", err)
			}
			assertPinnedManagedRuntimeDirs(t, recorded, staleDir, runtimeDirectory, tc.parentExists)
			log := string(mustReadTestFile(t, record))
			assertContainsAll(t, "managed runtime uv log", log, []string{
				"POST_CLEAR_PWD=",
				"ARGS=venv --python 3.14 --managed-python --clear " + runtimeDirectory,
				"ARGS=pip install --python " + runtimePythonPath(runtimeDirectory),
			})
		})
	}
}

// assertPinnedManagedRuntimeDirs verifies uv commands ran in an existing directory outside staleDir and target.
func assertPinnedManagedRuntimeDirs(
	t *testing.T, recorded []*exec.Cmd, staleDir, runtimeDirectory string, parentExists bool,
) {
	t.Helper()
	// Expect both uv venv and uv pip install calls to be recorded.
	if len(recorded) != 2 {
		t.Fatalf("expected 2 uv invocations, got %d", len(recorded))
	}
	for index, cmd := range recorded {
		// Dir must always be an explicit existing directory outside both the stale caller CWD and cleared target.
		if cmd.Dir == "" || cmd.Dir == staleDir || cmd.Dir == runtimeDirectory {
			t.Fatalf("command %d (%v) used invalid Dir=%q (stale=%q target=%q)", index, cmd.Args, cmd.Dir, staleDir, runtimeDirectory)
		}
		wantDir := filepath.Dir(cmd.Path)
		// Prefer the target's parent directory when it already existed on disk.
		if parentExists {
			wantDir = filepath.Dir(runtimeDirectory)
		}
		if cmd.Dir != wantDir {
			t.Fatalf("command %d used Dir=%q, want %q", index, cmd.Dir, wantDir)
		}
	}
}

// TestResolveUVPrefersSystemUVAndHARNESTUVBeforeEmbedded verifies system uv is used before embedded uv.
func TestResolveUVPrefersSystemUVAndHARNESTUVBeforeEmbedded(t *testing.T) {
	sys := defaultSystem()
	embeddedCalled := false
	sys.embeddedUV = func() (uvbootstrap.Artifact, error) {
		embeddedCalled = true
		return uvbootstrap.Artifact{Name: "uv", Contents: []byte("embedded")}, nil
	}
	sys.lookPath = func(name string) (string, error) {
		if name == "uv" {
			return "/usr/local/bin/uv", nil
		}
		if name == "/opt/corp/uv" {
			return "/opt/corp/uv", nil
		}
		return "", os.ErrNotExist
	}
	app := &application{system: sys, version: "test-version"}
	uvPath, cleanup, err := app.resolveUV()
	if err != nil {
		t.Fatalf("resolveUV failed: %v", err)
	}
	defer cleanup()
	if uvPath != "/usr/local/bin/uv" || embeddedCalled {
		t.Fatalf("got uv=%q embeddedCalled=%v, want system uv without embedded extraction", uvPath, embeddedCalled)
	}
	// Explicit HARNEST_UV must take precedence over PATH uv.
	t.Setenv("HARNEST_UV", "/opt/corp/uv")
	overridePath, cleanupOverride, err := app.resolveUV()
	if err != nil || overridePath != "/opt/corp/uv" || embeddedCalled {
		t.Fatalf("got override=%q err=%v embeddedCalled=%v, want /opt/corp/uv", overridePath, err, embeddedCalled)
	}
	cleanupOverride()
}

// TestRuntimeInstallUsesSystemUVWithHostPythonAndRespectsUVConfig verifies runtime install uses system uv and honors UV_INDEX_URL / PIP_INDEX_URL.
func TestRuntimeInstallUsesSystemUVWithHostPythonAndRespectsUVConfig(t *testing.T) {
	record := filepath.Join(t.TempDir(), "system-uv-runtime.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	t.Setenv("UV_INDEX_URL", "")
	t.Setenv("UV_DEFAULT_INDEX", "")
	t.Setenv("PIP_INDEX_URL", "https://artifactory.internal/simple")

	hostPython := writeExecutable(t, "#!/bin/sh\nexit 0\n")
	systemUV := writeExecutable(t, `#!/bin/sh
printf 'SYSUV\tNO_CONFIG=%s\tDEFAULT_INDEX=%s\tARGS=%s\n' "${UV_NO_CONFIG:-}" "${UV_DEFAULT_INDEX:-}" "$*" >> "$HARNEST_TEST_RECORD"
if [ "$1" = "venv" ]; then
  for target in "$@"; do :; done
  /bin/mkdir -p "$target/bin"
  printf '%s\n' '#!/bin/sh' > "$target/bin/python"
  /bin/chmod 0755 "$target/bin/python"
fi
`)
	sys := defaultSystem()
	sys.embeddedWheel = testEmbeddedWheel(t)
	sys.embeddedUV = func() (uvbootstrap.Artifact, error) {
		t.Fatal("embeddedUV should not be called when system uv is on PATH")
		return uvbootstrap.Artifact{}, nil
	}
	sys.lookPath = func(name string) (string, error) {
		if name == "uv" {
			return systemUV, nil
		}
		if name == hostPython {
			return hostPython, nil
		}
		return "", os.ErrNotExist
	}
	runtimeDirectory := filepath.Join(t.TempDir(), "runtime")
	if _, _, err := executeForTest(t, sys, "runtime", "install", "--bootstrap-python", hostPython, "--directory", runtimeDirectory); err != nil {
		t.Fatalf("runtime install with system uv failed: %v", err)
	}
	calls := string(mustReadTestFile(t, record))
	assertContainsAll(t, "system uv runtime install", calls, []string{
		"NO_CONFIG=\tDEFAULT_INDEX=https://artifactory.internal/simple",
		"ARGS=venv --python " + hostPython + " --clear " + runtimeDirectory,
		"ARGS=pip install --python " + runtimePythonPath(runtimeDirectory) + " --upgrade ",
	})
}

// TestRuntimeInstallManagedPythonUsesSystemUVAndRespectsUVIndexPrecedence verifies system uv is used for managed Python and UV_INDEX_URL takes precedence over PIP_INDEX_URL.
func TestRuntimeInstallManagedPythonUsesSystemUVAndRespectsUVIndexPrecedence(t *testing.T) {
	record := filepath.Join(t.TempDir(), "managed-system-uv.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	t.Setenv("UV_INDEX_URL", "https://uv-index.internal/simple")
	t.Setenv("UV_DEFAULT_INDEX", "")
	t.Setenv("PIP_INDEX_URL", "https://pip-index.internal/simple")

	systemUV := writeExecutable(t, `#!/bin/sh
printf 'SYSUV\tUV_INDEX=%s\tDEFAULT_INDEX=%s\tARGS=%s\n' "${UV_INDEX_URL:-}" "${UV_DEFAULT_INDEX:-}" "$*" >> "$HARNEST_TEST_RECORD"
if [ "$1" = "venv" ]; then
  for target in "$@"; do :; done
  /bin/mkdir -p "$target/bin"
  printf '%s\n' '#!/bin/sh' > "$target/bin/python"
  /bin/chmod 0755 "$target/bin/python"
fi
`)
	sys := defaultSystem()
	sys.embeddedWheel = testEmbeddedWheel(t)
	sys.embeddedUV = func() (uvbootstrap.Artifact, error) {
		t.Fatal("embeddedUV should not be called when system uv is available")
		return uvbootstrap.Artifact{}, nil
	}
	// Expose only system uv and no compatible host Python so managed Python installs via system uv.
	sys.lookPath = func(name string) (string, error) {
		if name == "uv" {
			return systemUV, nil
		}
		return "", os.ErrNotExist
	}
	runtimeDirectory := filepath.Join(t.TempDir(), "runtime")
	stdout, _, err := executeForTest(t, sys, "runtime", "install", "--directory", runtimeDirectory)
	// Managed Python installation must succeed via system uv without touching embedded uv.
	if err != nil {
		t.Fatalf("managed runtime install with system uv failed: %v", err)
	}
	assertContainsAll(t, "managed system uv output", stdout, []string{
		"installing managed Python 3.14 with system uv (" + systemUV + ")",
	})
	calls := string(mustReadTestFile(t, record))
	assertContainsAll(t, "managed system uv calls", calls, []string{
		"UV_INDEX=https://uv-index.internal/simple\tDEFAULT_INDEX=\t",
		"ARGS=venv --python 3.14 --managed-python --clear " + runtimeDirectory,
	})
}
