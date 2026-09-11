package main

import (
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// TestInitWrittenFilesExcludeRetiredProvider covers actual CLI output, including
// ignored samples, folder guides, and files added outside the scaffold map.
func TestInitWrittenFilesExcludeRetiredProvider(t *testing.T) {
	for _, framework := range []string{"adk", "langgraph"} {
		for _, mode := range []string{"managed", "advanced"} {
			for _, profile := range []string{"guided", "minimal", "example"} {
				t.Run(framework+"/"+mode+"/"+profile, func(t *testing.T) {
					target := filepath.Join(t.TempDir(), "neutral-agent")
					arguments := []string{"init", target, "--framework", framework, "--mode", mode}
					if profile != "guided" {
						arguments = append(arguments, "--"+profile)
					}
					stdout, stderr, err := executeForTest(t, defaultSystem(), arguments...)
					if err != nil {
						t.Fatal(err)
					}
					assertNoOllamaReference(t, "command output", stdout+stderr)
					assertGeneratedTreeExcludesRetiredProvider(t, target)
				})
			}
		}
	}
}

// TestInitDefaultsExcludeRetiredProvider also exercises omitted selection flags.
func TestInitDefaultsExcludeRetiredProvider(t *testing.T) {
	target := filepath.Join(t.TempDir(), "default-agent")
	stdout, stderr, err := executeForTest(t, defaultSystem(), "init", target)
	if err != nil {
		t.Fatal(err)
	}
	assertNoOllamaReference(t, "command output", stdout+stderr)
	assertGeneratedTreeExcludesRetiredProvider(t, target)
}

// assertGeneratedTreeExcludesRetiredProvider scans hidden and ignored files too;
// they are authored guidance even when the runtime does not load them.
func assertGeneratedTreeExcludesRetiredProvider(t *testing.T, root string) {
	t.Helper()
	err := fs.WalkDir(os.DirFS(root), ".", func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		assertNoOllamaReference(t, path, "")
		if !entry.IsDir() {
			contents := mustReadTestFile(t, filepath.Join(root, path))
			assertNoOllamaReference(t, path, string(contents))
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

// TestRepositorySamplesExcludeRetiredProvider checks every versioned sample,
// excluding only untracked environments and build artifacts through Git's index.
func TestRepositorySamplesExcludeRetiredProvider(t *testing.T) {
	root := filepath.Join("..", "..")
	command := exec.Command("git", "ls-files", "-z", "--", "examples")
	command.Dir = root
	output, err := command.Output()
	if err != nil {
		t.Fatal(err)
	}
	paths := strings.Split(strings.TrimSuffix(string(output), "\x00"), "\x00")
	if len(paths) == 0 || paths[0] == "" {
		t.Fatal("no versioned samples found")
	}
	for _, path := range paths {
		contents := mustReadTestFile(t, filepath.Join(root, path))
		assertNoOllamaReference(t, path, string(contents))
	}
}

// assertNoOllamaReference rejects any mention, not just connector or env names.
func assertNoOllamaReference(t *testing.T, path, contents string) {
	t.Helper()
	if strings.Contains(strings.ToLower(path+"\n"+contents), "ollama") {
		t.Errorf("sample or generated file %s mentions the retired provider", path)
	}
}
