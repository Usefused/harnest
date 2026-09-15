package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDoctorInfersAgentFrameworkAndPublishedEnvironment(t *testing.T) {
	for _, framework := range []string{"adk", "langgraph"} {
		for _, profile := range environmentProfiles {
			t.Run(framework+"/"+string(profile), func(t *testing.T) {
				directory := filepath.Join(t.TempDir(), "agent")
				if err := createScaffoldForFramework(directory, "agent", framework); err != nil {
					t.Fatal(err)
				}
				python := seedDoctorEnvironment(t, directory, profile)
				app := application{system: defaultSystem()}
				t.Setenv("HARNEST_PYTHON", "")
				selected, detected, err := app.doctorPython([]string{directory}, "")
				if err != nil || selected.Executable != python || detected != framework {
					t.Fatalf("selection=%+v framework=%s error=%v", selected, detected, err)
				}
			})
		}
	}
}

// seedDoctorEnvironment publishes a disposable interpreter in a real profile layout.
func seedDoctorEnvironment(t *testing.T, directory string, profile environmentProfile) string {
	t.Helper()
	root := filepath.Join(directory, ".harnest")
	python := runtimePythonPath(filepath.Join(root, "environments", "0123456789abcdef"))
	if err := os.MkdirAll(filepath.Dir(python), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(python, []byte("test interpreter"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := writeEnvironmentState(filepath.Join(root, profile.stateFile()), environmentState{
		Fingerprint: "0123456789abcdef", Directory: "environments/0123456789abcdef",
	}); err != nil {
		t.Fatal(err)
	}
	return python
}

func TestDoctorDetectsCurrentAgentDirectory(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "agent")
	if err := createScaffold(directory, "agent"); err != nil {
		t.Fatal(err)
	}
	previous, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(directory); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(previous) })
	detected, err := doctorProjectDirectory(nil)
	want, resolveErr := filepath.EvalSymlinks(directory)
	if err != nil || resolveErr != nil || detected != want {
		t.Fatalf("detected=%s error=%v", detected, err)
	}
}

func TestDoctorMissingAgentEnvironmentSuggestsSync(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "agent")
	if err := createScaffold(directory, "agent"); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HARNEST_PYTHON", "")
	stdout, _, err := executeForTest(t, defaultSystem(), "doctor", directory)
	if err == nil || !strings.Contains(stdout, "harnest env sync") {
		t.Fatalf("error=%v output=%s", err, stdout)
	}
	if _, err := os.Stat(filepath.Join(directory, ".harnest")); !os.IsNotExist(err) {
		t.Fatalf("doctor changed the missing environment: %v", err)
	}
}

func TestDoctorProbeOnlySelectsExplicitFramework(t *testing.T) {
	python := writeExecutable(t, `#!/bin/sh
printf '%s\n' "{\"executable\":\"/runtime/python\",\"python\":\"3.12.8\",\"supported\":true,\"packages\":[{\"name\":\"selected-framework:$3\",\"ok\":true,\"version\":\"test\",\"error\":\"\"}]}"
`)
	for _, framework := range []string{"", "adk", "langgraph"} {
		args := []string{"--python", python, "doctor"}
		if framework != "" {
			args = append(args, "--framework", framework)
		}
		stdout, _, err := executeForTest(t, defaultSystem(), args...)
		if err != nil || !strings.Contains(stdout, "selected-framework:"+framework+" test") {
			t.Fatalf("framework=%s error=%v output=%s", framework, err, stdout)
		}
	}
}

func TestServeDefaultPort(t *testing.T) {
	app := application{system: defaultSystem()}
	if got := app.newServeCommand().Flags().Lookup("port").DefValue; got != "1907" {
		t.Fatalf("default serve port = %s", got)
	}
}
