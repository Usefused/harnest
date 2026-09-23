package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestProjectPackInitCreatesEditableCompileTemplates exercises the public CLI without acquiring Python or dependencies.
func TestProjectPackInitCreatesEditableCompileTemplates(t *testing.T) {
	output := filepath.Join(t.TempDir(), "company pack")
	stdout, _, err := executeForTest(t, defaultSystem(), "pack", "init", "acme", "--output", output)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout, compileSelectionFile) {
		t.Fatalf("missing compile declaration path: %s", stdout)
	}
	assertFilesExist(t, output, []string{"pack.py", compileSelectionFile, "team-guide.md"})
	selection, err := readCompileSelection(output)
	if err != nil {
		t.Fatal(err)
	}
	if len(selection.Extras) != 0 {
		t.Fatalf("bad generated selection: %#v", selection)
	}
}

// TestProjectPackInitRefusesExistingDestination preserves edited team code even when init is repeated.
func TestProjectPackInitRefusesExistingDestination(t *testing.T) {
	output := filepath.Join(t.TempDir(), "acme-pack")
	if err := createProjectPackScaffold(output, "acme"); err != nil {
		t.Fatal(err)
	}
	mustWriteEnvironmentFixture(t, filepath.Join(output, "pack.py"), "team-owned edits")
	if _, _, err := executeForTest(t, defaultSystem(), "pack", "init", "acme", "--output", output); err == nil {
		t.Fatal("overwrote existing pack")
	}
	if string(mustReadTestFile(t, filepath.Join(output, "pack.py"))) != "team-owned edits" {
		t.Fatal("changed team-owned pack")
	}
}

// TestProjectPackInitRejectsInvalidNames validates identity before creating destination directories.
func TestProjectPackInitRejectsInvalidNames(t *testing.T) {
	for _, name := range []string{"harnest", "../escape", "bad_name", "Bad", strings.Repeat("a", 64)} {
		t.Run(name, func(t *testing.T) {
			output := filepath.Join(t.TempDir(), "pack")
			if _, _, err := executeForTest(t, defaultSystem(), "pack", "init", name, "--output", output); err == nil {
				t.Fatal("invalid pack name accepted")
			}
			if _, err := os.Lstat(output); !os.IsNotExist(err) {
				t.Fatal("invalid identity created a directory")
			}
		})
	}
}

// TestProjectPackInitDefaultOutput uses the pack identity without requiring a redundant path flag.
func TestProjectPackInitDefaultOutput(t *testing.T) {
	t.Chdir(t.TempDir())
	if _, _, err := executeForTest(t, defaultSystem(), "pack", "init", "acme"); err != nil {
		t.Fatal(err)
	}
	assertFilesExist(t, "acme-pack", []string{"pack.py", compileSelectionFile})
}
