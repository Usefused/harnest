package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/engine"
)

func TestServeArtifactCacheReusesContentAndPrunesSupersededGeneration(t *testing.T) {
	project := filepath.Join(t.TempDir(), "cached-agent")
	if err := createScaffold(project, "cached-agent"); err != nil {
		t.Fatal(err)
	}
	record := filepath.Join(t.TempDir(), "compiles.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	python := writeExecutable(t, artifactCacheTestPython)
	sys := defaultSystem()
	sys.loadCompiledArtifact = testCompiledArtifactLoader
	app := &application{system: sys, version: "0.14.1"}
	command := artifactCacheTestCommand()
	selection := pythonSelection{Executable: python, Source: "agent environment"}

	firstBundle := mustLoadArtifactCacheBundle(t, project)
	first := prepareTestServeArtifact(
		t, app, command, firstBundle, selection, false,
	)
	second := prepareTestServeArtifact(
		t, app, command, firstBundle, selection, true,
	)
	if second != first {
		t.Fatalf("cache hit = %q, want %q", second, first)
	}
	assertCompileCount(t, record, 1)

	instructions := filepath.Join(project, "instructions.md")
	if err := os.WriteFile(instructions, []byte("Changed graph content\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	changedBundle := mustLoadArtifactCacheBundle(t, project)
	third := prepareTestServeArtifact(
		t, app, command, changedBundle, selection, false,
	)
	if third == first {
		t.Fatalf("changed compile reused %q", first)
	}
	assertCompileCount(t, record, 2)
	waitForArtifactRemoval(t, first)
	if _, err := os.Stat(filepath.Join(third, "harnest-agent")); err != nil {
		t.Fatalf("current cached artifact is unavailable: %v", err)
	}
}

// prepareTestServeArtifact checks one expected hit or miss and returns its path.
func prepareTestServeArtifact(
	t *testing.T,
	app *application,
	command *cobra.Command,
	bundle engine.Bundle,
	selection pythonSelection,
	wantCached bool,
) string {
	t.Helper()
	artifact, cached, err := app.prepareCachedServeArtifact(
		command, bundle, selection,
	)
	if err != nil {
		t.Fatal(err)
	}
	if cached != wantCached {
		t.Fatalf("cached = %t, want %t", cached, wantCached)
	}
	return artifact
}

// mustLoadArtifactCacheBundle resolves a complete digest for cache assertions.
func mustLoadArtifactCacheBundle(t *testing.T, project string) engine.Bundle {
	t.Helper()
	bundle, err := loadAgentBundle(project)
	if err != nil {
		t.Fatal(err)
	}
	return bundle
}

func TestStaleServeArtifactsIgnoreUnownedEntries(t *testing.T) {
	root := t.TempDir()
	current := strings.Repeat("a", 64)
	stale := strings.Repeat("b", 64)
	for _, name := range []string{current, stale, "notes"} {
		if err := os.Mkdir(filepath.Join(root, name), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	link := strings.Repeat("c", 64)
	if err := os.Symlink(filepath.Join(root, stale), filepath.Join(root, link)); err != nil {
		t.Fatal(err)
	}

	selected := staleServeArtifacts(root, current)
	if len(selected) != 1 || selected[0] != filepath.Join(root, stale) {
		t.Fatalf("stale artifacts = %#v", selected)
	}
}

func TestServeArtifactCacheRecompilesCorruptCurrentGeneration(t *testing.T) {
	project := filepath.Join(t.TempDir(), "corrupt-agent")
	if err := createScaffold(project, "corrupt-agent"); err != nil {
		t.Fatal(err)
	}
	record := filepath.Join(t.TempDir(), "compiles.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	selection := pythonSelection{
		Executable: writeExecutable(t, artifactCacheTestPython),
		Source:     "agent environment",
	}
	sys := defaultSystem()
	sys.loadCompiledArtifact = testCompiledArtifactLoader
	app := &application{system: sys, version: "0.14.1"}
	bundle := mustLoadArtifactCacheBundle(t, project)
	command := artifactCacheTestCommand()
	artifact := prepareTestServeArtifact(
		t, app, command, bundle, selection, false,
	)
	if err := os.Remove(filepath.Join(artifact, "harnest-agent")); err != nil {
		t.Fatal(err)
	}
	repaired := prepareTestServeArtifact(
		t, app, command, bundle, selection, false,
	)
	if repaired != artifact {
		t.Fatalf("repair moved current generation from %q to %q", artifact, repaired)
	}
	assertCompileCount(t, record, 2)
}

func TestServeArtifactCacheKeepsPreviousGenerationAfterCompileFailure(t *testing.T) {
	project := filepath.Join(t.TempDir(), "failed-agent")
	if err := createScaffold(project, "failed-agent"); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HARNEST_TEST_RECORD", filepath.Join(t.TempDir(), "compiles.txt"))
	selection := pythonSelection{
		Executable: writeExecutable(t, artifactCacheTestPython),
		Source:     "agent environment",
	}
	sys := defaultSystem()
	sys.loadCompiledArtifact = testCompiledArtifactLoader
	app := &application{system: sys, version: "0.14.1"}
	command := artifactCacheTestCommand()
	original := prepareTestServeArtifact(
		t,
		app,
		command,
		mustLoadArtifactCacheBundle(t, project),
		selection,
		false,
	)
	if err := os.WriteFile(
		filepath.Join(project, "instructions.md"), []byte("Changed\n"), 0o644,
	); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HARNEST_TEST_COMPILE_FAIL", "1")
	_, _, err := app.prepareCachedServeArtifact(
		command, mustLoadArtifactCacheBundle(t, project), selection,
	)
	if err == nil {
		t.Fatal("failed compiler unexpectedly published a generation")
	}
	if _, err := os.Stat(filepath.Join(original, "harnest-agent")); err != nil {
		t.Fatalf("previous generation was removed after failure: %v", err)
	}
}

// artifactCacheTestCommand supplies only the process boundaries compilation needs.
func artifactCacheTestCommand() *cobra.Command {
	command := &cobra.Command{}
	command.SetContext(context.Background())
	command.SetIn(strings.NewReader(""))
	command.SetOut(io.Discard)
	command.SetErr(io.Discard)
	return command
}

// testCompiledArtifactLoader models validation without importing authored Python.
func testCompiledArtifactLoader(
	directory string, _ engine.Bundle,
) (engine.CompiledArtifact, error) {
	launcher := filepath.Join(directory, "harnest-agent")
	info, err := os.Stat(launcher)
	if err != nil || !info.Mode().IsRegular() {
		return engine.CompiledArtifact{}, fmt.Errorf("invalid test artifact")
	}
	return engine.CompiledArtifact{Directory: directory}, nil
}

// assertCompileCount proves a cache hit never enters the Python compiler.
func assertCompileCount(t *testing.T, path string, expected int) {
	t.Helper()
	contents, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if count := len(strings.Fields(string(contents))); count != expected {
		t.Fatalf("compile count = %d, want %d", count, expected)
	}
}

// waitForArtifactRemoval bounds observation of background cache reclamation.
func waitForArtifactRemoval(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Lstat(path); os.IsNotExist(err) {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("superseded artifact was not removed: %s", path)
}

const artifactCacheTestPython = `#!/bin/sh
printf 'compile\n' >> "$HARNEST_TEST_RECORD"
if [ "$HARNEST_TEST_COMPILE_FAIL" = "1" ]; then exit 9; fi
output=""
previous=""
for value in "$@"; do
  if [ "$previous" = "--output" ]; then output="$value"; fi
  previous="$value"
done
mkdir -p "$output"
printf 'generated launcher\n' > "$output/harnest-agent"
`
