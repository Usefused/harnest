package main

import (
	"encoding/json"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"harnest.dev/harnest/engine"
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

// TestInitAgentPluginExampleUsesPortableSkillsOnlyFormat keeps activation offline
// while teaching the standard manifest instead of executable MCP factories.
func TestInitAgentPluginExampleUsesPortableSkillsOnlyFormat(t *testing.T) {
	files := scaffoldFilesForMode("example-agent", "adk", "managed", true)
	var manifest map[string]string
	if err := json.Unmarshal([]byte(files["plugins/_example_agent/plugin.json"]), &manifest); err != nil {
		t.Fatalf("example plugin.json is invalid JSON: %v", err)
	}
	if manifest["$schema"] != "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json" || manifest["name"] != "starter" {
		t.Fatalf("unexpected Agent Plugins manifest: %#v", manifest)
	}
	for path := range files {
		if strings.HasPrefix(path, "plugins/") && (strings.HasSuffix(path, ".py") || strings.HasSuffix(path, "mcp.json")) {
			t.Fatalf("skills-only sample must not activate an MCP connection or Python factory: %s", path)
		}
	}
	assertContainsAll(t, "Agent Plugins guide", files["plugins/_README.md"], []string{
		"skills-only Agent Plugin", "either or both", "starter/mcp.json",
		"https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
		`"type": "streamable-http"`, `"url": "https://mcp.example.com/mcp"`,
	})
}

// TestInitAgentPluginExamplesAreAbsentWithoutManagedExample prevents manifests
// from leaking out of the ignored, explicitly requested sample profile.
func TestInitAgentPluginExamplesAreAbsentWithoutManagedExample(t *testing.T) {
	for _, mode := range []string{"managed", "advanced"} {
		for _, example := range []bool{false, true} {
			if mode == "managed" && example {
				continue
			}
			files := scaffoldFilesForMode("example-agent", "adk", mode, example)
			for path := range files {
				if strings.HasPrefix(path, "plugins/") && path != "plugins/_README.md" {
					t.Fatalf("mode=%s example=%t unexpectedly generated %s", mode, example, path)
				}
			}
		}
	}
}

// TestInitAgentPluginCanonicalAssetsPassBundleValidation checks that the Go
// boundary accepts standard manifests and ordinary plugin reference assets.
func TestInitAgentPluginCanonicalAssetsPassBundleValidation(t *testing.T) {
	root := filepath.Join(t.TempDir(), "example-agent")
	if _, _, err := executeForTest(t, defaultSystem(), "init", root, "--example"); err != nil {
		t.Fatal(err)
	}
	plugin := filepath.Join(root, "plugins", "starter")
	if err := os.Rename(filepath.Join(root, "plugins", "_example_agent"), plugin); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(plugin, "README.md"), []byte("# Starter plugin\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := engine.LoadBundle(root); err != nil {
		t.Fatalf("canonical Agent Plugin failed bundle validation: %v", err)
	}
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
