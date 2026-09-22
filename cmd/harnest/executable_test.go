package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/spf13/cobra"
	"harnest.dev/harnest/internal/agentpack"
)

// executableFixture isolates the real native launcher from Python and model-provider availability.
func executableFixture(t *testing.T, script string) (string, string, agentpack.Manifest) {
	t.Helper()
	root := t.TempDir()
	source := fixtureInterpreter(t, root, script)
	pack := filepath.Join(root, "pack")
	store := filepath.Join(pack, "objects")
	file, err := agentpack.StoreFile(source, store)
	if err != nil {
		t.Fatal(err)
	}
	file.Path = "python/bin/python3"
	if runtime.GOOS == "windows" {
		file.Path = "python/python.exe"
	}
	m := agentpack.Manifest{Format: agentpack.Format, OS: runtime.GOOS, Arch: runtime.GOARCH, Python: file.Path, Files: []agentpack.File{file}}
	m.Seal()
	if err := agentpack.WriteManifest(pack, m); err != nil {
		t.Fatal(err)
	}
	artifact := filepath.Join(root, "artifact")
	if err := os.MkdirAll(artifact, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(artifact, "agent.py"), []byte("placeholder"), 0600); err != nil {
		t.Fatal(err)
	}
	return pack, artifact, m
}

// TestNativeExecutableRunsAttachedAndEmbedded verifies actual native execution without PATH Python.
func TestNativeExecutableRunsAttachedAndEmbedded(t *testing.T) {
	command := &cobra.Command{}
	command.SetContext(context.Background())
	launcher, err := loadAgentLauncher(command)
	if err != nil {
		t.Fatal(err)
	}
	pack, artifact, m := executableFixture(t, "#!/bin/sh\nread message\nprintf '%s:%s:%s\\n' \"$message\" \"$PACK_DEFAULT\" \"$PACK_OVERRIDE\"\nexit 23\n")
	for _, embedded := range []bool{false, true} {
		output := filepath.Join(t.TempDir(), "agent.exe")
		defaults := map[string]string{"PACK_DEFAULT": "configured", "PACK_OVERRIDE": "source"}
		if err := agentpack.WriteExecutable(output, launcher, artifact, pack, m, embedded, defaults); err != nil {
			t.Fatal(err)
		}
		if err := signAgentExecutable(command, &application{system: defaultSystem()}, output); err != nil {
			t.Fatal(err)
		}
		args := []string{"run"}
		if !embedded {
			args = append([]string{"--runtime", pack}, args...)
		}
		child := exec.Command(output, args...)
		child.Env = []string{"PATH=/nonexistent", "HARNEST_AGENT_CACHE=" + t.TempDir(), "PACK_OVERRIDE=deployment"}
		child.Stdin = strings.NewReader("hello\n")
		result, err := child.CombinedOutput()
		exit, ok := err.(*exec.ExitError)
		if !ok || exit.ExitCode() != 23 || string(result) != "hello:configured:deployment\n" {
			t.Fatalf("native launch: %v: %s", err, result)
		}
	}
}

// TestNativeExecutableRejectsMissingOrWrongRuntime proves rejection before dependency execution.
func TestNativeExecutableRejectsMissingOrWrongRuntime(t *testing.T) {
	command := &cobra.Command{}
	command.SetContext(context.Background())
	launcher, err := loadAgentLauncher(command)
	if err != nil {
		t.Fatal(err)
	}
	pack, artifact, m := executableFixture(t, "#!/bin/sh\necho should-not-execute\n")
	output := filepath.Join(t.TempDir(), "agent.exe")
	if err := agentpack.WriteExecutable(output, launcher, artifact, pack, m, false, nil); err != nil {
		t.Fatal(err)
	}
	if err := signAgentExecutable(command, &application{system: defaultSystem()}, output); err != nil {
		t.Fatal(err)
	}
	result, err := exec.Command(output, "serve").CombinedOutput()
	if err == nil || !strings.Contains(string(result), "attach runtime") {
		t.Fatalf("missing attachment: %v: %s", err, result)
	}
	m.Inputs = []string{"changed"}
	m.Seal()
	if err := agentpack.WriteManifest(pack, m); err != nil {
		t.Fatal(err)
	}
	result, err = exec.Command(output, "--runtime", pack, "serve").CombinedOutput()
	if err == nil || !strings.Contains(string(result), "does not match required") {
		t.Fatalf("wrong attachment: %v: %s", err, result)
	}
}

// TestExecutableOptions keeps reference and embedding behavior explicit and backwards compatible.
func TestExecutableOptions(t *testing.T) {
	for _, options := range []executableOptions{{runtime: "pack"}, {embed: true}, {format: "executable"}} {
		if !options.enabled() || options.validate() != nil {
			t.Fatal("executable options rejected")
		}
	}
	if (executableOptions{}).enabled() {
		t.Fatal("default compile changed format")
	}
	for _, options := range []executableOptions{{runtime: "pack", format: "directory"}, {format: "unknown"}} {
		if options.validate() == nil {
			t.Fatal("invalid format accepted")
		}
	}
}

// fixtureInterpreter builds a genuine PE executable on Windows so process tests
// exercise native loading and appended payloads instead of relying on a shell.
func fixtureInterpreter(t *testing.T, root, script string) string {
	t.Helper()
	source := filepath.Join(root, "python")
	if runtime.GOOS == "windows" {
		source += ".exe"
		if data, err := exec.Command("go", "build", "-o", source, "./testdata/pack-interpreter").CombinedOutput(); err != nil {
			t.Fatalf("build interpreter fixture: %v: %s", err, data)
		}
	} else if err := os.WriteFile(source, []byte(script), 0755); err != nil {
		t.Fatal(err)
	}
	return source
}
