package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"harnest.dev/harnest/engine"
)

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
