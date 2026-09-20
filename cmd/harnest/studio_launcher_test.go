package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"
	"harnest.dev/harnest/internal/runtimewheel"
	"harnest.dev/harnest/internal/uvbootstrap"
)

// TestStudioDefaultsToCallerDirectory verifies the subprocess receives cwd, not a cache/project default.
func TestStudioDefaultsToCallerDirectory(t *testing.T) {
	record := filepath.Join(t.TempDir(), "argv")
	t.Setenv("HARNEST_STUDIO_TEST_RECORD", record)
	python := writeExecutable(t, "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$HARNEST_STUDIO_TEST_RECORD\"\nprintf 'PYTHON=%s\\n' \"$HARNEST_PYTHON\" >> \"$HARNEST_STUDIO_TEST_RECORD\"\n")
	_, _, err := executeForTest(t, defaultSystem(), "--python", python, "studio")
	if err != nil {
		t.Fatal(err)
	}
	cwd, _ := os.Getwd()
	executable, _ := os.Executable()
	assertContainsAll(t, "Studio invocation", string(mustReadTestFile(t, record)), []string{
		"-m\nharnest_builder\n", "--workspace\n" + cwd, "--port\n1940", "--cli\n" + executable, "PYTHON=" + python,
	})
	selected := filepath.Join(t.TempDir(), "workspace with spaces")
	if err := os.Mkdir(selected, 0o700); err != nil {
		t.Fatal(err)
	}
	_, _, err = executeForTest(t, defaultSystem(), "--python", python, "studio", "--workspace", selected, "--port", "2940")
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "explicit Studio workspace", string(mustReadTestFile(t, record)), []string{"--workspace\n" + selected, "--port\n2940"})
}

// TestStudioRejectsInvalidLaunchBeforeBootstrapping protects unrelated directories and avoids unnecessary installs.
func TestStudioRejectsInvalidLaunchBeforeBootstrapping(t *testing.T) {
	for _, arguments := range [][]string{{"--port", "80"}, {"--port", "65536"}, {"--workspace", filepath.Join(t.TempDir(), "absent")}, {"unexpected"}} {
		_, _, err := executeForTest(t, defaultSystem(), append([]string{"studio"}, arguments...)...)
		if err == nil {
			t.Fatalf("accepted invalid arguments: %v", arguments)
		}
	}
}

// TestStudioCachesOnlyCompleteReleaseRuntime verifies bootstrap, wheel extras, reuse, and version isolation.
func TestStudioCachesOnlyCompleteReleaseRuntime(t *testing.T) {
	root := t.TempDir()
	calls := filepath.Join(root, "calls")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	app := &application{system: environmentTestSystem(t, root), version: "0.21.3"}
	command := &cobra.Command{}
	command.SetContext(context.Background())
	wheel := runtimewheel.Artifact{Name: "harnest-test.whl", Contents: []byte("first release")}
	first, err := app.prepareStudioEnvironment(command, filepath.Join(root, "studio"), wheel)
	if err != nil {
		t.Fatal(err)
	}
	output := string(mustReadTestFile(t, calls))
	assertContainsAll(t, "Studio bootstrap", output, []string{"venv --python 3.12", "pip install --python", ".whl[studio]", "-I -c", "harnest-manifest.json"})
	second, err := app.prepareStudioEnvironment(command, filepath.Join(root, "studio"), wheel)
	if err != nil || first.Executable != second.Executable {
		t.Fatalf("cached runtime: %v, %v", second, err)
	}
	if string(mustReadTestFile(t, calls)) != output {
		t.Fatal("cached launch reinstalled dependencies")
	}
	wheel.Contents = []byte("next release")
	next, err := app.prepareStudioEnvironment(command, filepath.Join(root, "studio"), wheel)
	if err != nil || next.Executable == first.Executable {
		t.Fatalf("release isolation: %v, %v", next, err)
	}
	if _, err := os.Stat(first.Executable); err != nil {
		t.Fatal("upgrading removed a possibly running older Studio runtime")
	}
}

// TestStudioFailedInstallCannotPublishReadiness proves a retry cannot reuse an incomplete environment.
func TestStudioFailedInstallCannotPublishReadiness(t *testing.T) {
	root := t.TempDir()
	sys := defaultSystem()
	sys.embeddedUV = func() (uvbootstrap.Artifact, error) { return uvbootstrap.Artifact{}, errors.New("unavailable") }
	app := &application{system: sys}
	_, err := app.prepareStudioEnvironment(&cobra.Command{}, root, runtimewheel.Artifact{Contents: []byte("release")})
	if err == nil {
		t.Fatal("bootstrap failure was ignored")
	}
	if _, err := os.Stat(filepath.Join(root, "environment.json")); !os.IsNotExist(err) {
		t.Fatalf("published failed install: %v", err)
	}
}

// TestStudioReleasedBinaryDoesNotSilentlyUseUnbundledPython keeps release packaging failures visible.
func TestStudioReleasedBinaryDoesNotSilentlyUseUnbundledPython(t *testing.T) {
	sys := defaultSystem()
	sys.getenv = func(string) string { return "" }
	sys.embeddedWheel = func(string) (runtimewheel.Artifact, error) {
		return runtimewheel.Artifact{}, errors.New("missing wheel")
	}
	app := &application{system: sys, version: "0.21.3"}
	_, err := app.studioPython(&cobra.Command{})
	if err == nil || !strings.Contains(err.Error(), "bundled Studio") {
		t.Fatalf("unexpected error %v", err)
	}
}
