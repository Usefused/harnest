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
	assertFilesExist(t, output, []string{packManifestFile, "pack.py", "templates/" + compileSelectionFile, "templates/team-guide.md"})
	manifest := string(mustReadTestFile(t, filepath.Join(output, packManifestFile)))
	assertContainsAll(t, "pack manifest", manifest, []string{"apiVersion: harnest.dev/pack/v1", "name: acme", "command: acme"})
	if strings.Contains(string(mustReadTestFile(t, filepath.Join(output, "pack.py"))), "ProjectCLI") {
		t.Fatal("scaffold still uses the removed ProjectCLI")
	}
	selection, err := readCompileSelection(filepath.Join(output, "templates"))
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
	assertFilesExist(t, "acme-pack", []string{packManifestFile, "pack.py", "templates/" + compileSelectionFile})
}

// recordingPython writes the Python arguments a command would run, one per line.
func recordingPython(t *testing.T) (string, string) {
	t.Helper()
	record := filepath.Join(t.TempDir(), "argv")
	t.Setenv("HARNEST_PACK_TEST_RECORD", record)
	return writeExecutable(t, "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$HARNEST_PACK_TEST_RECORD\"\n"), record
}

// TestInitAndUpgradeWithPackDelegateToOnePlanner sends pack runs through the combined Python planner.
func TestInitAndUpgradeWithPackDelegateToOnePlanner(t *testing.T) {
	pack := filepath.Join(t.TempDir(), "acme-pack")
	if err := createProjectPackScaffold(pack, "acme"); err != nil {
		t.Fatal(err)
	}
	python, record := recordingPython(t)
	agent := filepath.Join(t.TempDir(), "agent")
	if _, _, err := executeForTest(t, defaultSystem(), "--python", python, "init", agent, "--pack", pack, "--option", "team=support", "--framework", "langgraph", "--dry-run"); err != nil {
		t.Fatal(err)
	}
	executable, _ := os.Executable()
	initArgs := string(mustReadTestFile(t, record))
	assertContainsAll(t, "pack init", initArgs, []string{
		"-m\nharnest.cli\npack\ninit\n" + agent + "\n--harnest\n" + executable,
		"--pack\n" + pack, "--option\nteam=support", "--framework\nlanggraph", "--dry-run",
	})
	if strings.Contains(initArgs, "--mode") {
		t.Fatal("forwarded an unchanged default scaffold flag")
	}
	if _, _, err := executeForTest(t, defaultSystem(), "--python", python, "upgrade", agent, "--pack", pack, "--apply"); err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "pack upgrade", string(mustReadTestFile(t, record)), []string{
		"-m\nharnest.cli\npack\nupgrade\n" + agent, "--pack\n" + pack, "--apply",
	})
}

// TestPackOnlyFlagsRequirePack keeps plain init and upgrade unchanged.
func TestPackOnlyFlagsRequirePack(t *testing.T) {
	for _, arguments := range [][]string{
		{"init", filepath.Join(t.TempDir(), "a"), "--option", "team=x"},
		{"init", filepath.Join(t.TempDir(), "b"), "--dry-run"},
		{"upgrade", t.TempDir(), "--json"},
	} {
		if _, _, err := executeForTest(t, defaultSystem(), arguments...); err == nil || !strings.Contains(err.Error(), "requires --pack") {
			t.Fatalf("%v: %v", arguments, err)
		}
	}
	if _, _, err := executeForTest(t, defaultSystem(), "init", filepath.Join(t.TempDir(), "c"), "--pack", t.TempDir()); err == nil || !strings.Contains(err.Error(), "pack.yaml") {
		t.Fatalf("accepted a folder without pack.yaml: %v", err)
	}
}

// TestPackRunPassesCompanyFlagsThrough leaves a company command's own flags untouched.
func TestPackRunPassesCompanyFlagsThrough(t *testing.T) {
	pack := filepath.Join(t.TempDir(), "acme-pack")
	if err := createProjectPackScaffold(pack, "acme"); err != nil {
		t.Fatal(err)
	}
	python, record := recordingPython(t)
	t.Setenv("HARNEST_PYTHON", python)
	if _, _, err := executeForTest(t, defaultSystem(), "pack", "run", pack, "check", "--strict", "-v"); err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "pack run", string(mustReadTestFile(t, record)), []string{"pack\nrun\n" + pack + "\ncheck\n--strict\n-v"})
	output := filepath.Join(t.TempDir(), "dist")
	if _, _, err := executeForTest(t, defaultSystem(), "pack", "build", pack, "--output", output); err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "pack build", string(mustReadTestFile(t, record)), []string{"pack\nbuild\n" + pack + "\n--output\n" + output})
}
