package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestTaskPreflightExplainsMisplacedFile(t *testing.T) {
	root := t.TempDir()
	directory := filepath.Join(root, "tasks")
	if err := os.Mkdir(directory, 0755); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(directory, "notes.md")
	if err := os.WriteFile(path, []byte("notes"), 0600); err != nil {
		t.Fatal(err)
	}
	_, err := hasAuthoredTasks(root)
	if err == nil {
		t.Fatal("misplaced file was accepted")
	}
	for _, expected := range []string{path, "What Harnest expects:", "How to fix:", "_notes.md", "real Python"} {
		if !strings.Contains(err.Error(), expected) {
			t.Errorf("diagnostic missing %q: %s", expected, err)
		}
	}
	if err := os.Rename(path, filepath.Join(directory, "_notes.md")); err != nil {
		t.Fatal(err)
	}
	if found, err := hasAuthoredTasks(root); found || err != nil {
		t.Fatalf("inactive note should be ignored: found=%v err=%v", found, err)
	}
}

func TestPluginPreflightExplainsLooseFile(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "notes.md")
	if err := os.WriteFile(path, []byte("notes"), 0600); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(directory)
	if err != nil {
		t.Fatal(err)
	}
	_, _, _, err = pluginRuntimeProject(directory, entries[0], false)
	if err == nil {
		t.Fatal("loose plugin file was accepted")
	}
	for _, expected := range []string{path, "one subfolder per plugin", "How to fix:", "_notes.md"} {
		if !strings.Contains(err.Error(), expected) {
			t.Errorf("diagnostic missing %q: %s", expected, err)
		}
	}
}
