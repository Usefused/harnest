package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAgentPluginInstallCopiesLocalPackageByManifestIdentity(t *testing.T) {
	project := agentPluginTestProject(t)
	source := filepath.Join(t.TempDir(), "downloaded-folder")
	writeAgentPluginFixture(t, source, `{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "portable.tools",
  "description": "Portable test"
}`)
	script := filepath.Join(source, "bin", "server")
	if err := os.MkdirAll(filepath.Dir(script), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(script, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	// Portable packages are data during installation, even when they contain Python.
	if err := os.WriteFile(filepath.Join(source, "plugin.py"), []byte("this is not valid Python\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	stdout, _, err := executeForTest(
		t, defaultSystem(), "agent-plugins", "install", source, "--project", project,
	)
	if err != nil {
		t.Fatal(err)
	}
	destination := filepath.Join(project, "agent-plugins", "portable.tools")
	assertContainsAll(t, "install output", stdout, []string{"portable.tools", destination})
	if got := string(mustReadTestFile(t, filepath.Join(destination, "plugin.py"))); !strings.Contains(got, "not valid") {
		t.Fatalf("installed plugin.py = %q", got)
	}
	info, err := os.Stat(filepath.Join(destination, "bin", "server"))
	if err != nil || info.Mode().Perm()&0o111 == 0 {
		t.Fatalf("executable mode was not preserved: %v, %v", info, err)
	}
}

func TestAgentPluginInstallRefusesReplacementUnlessForced(t *testing.T) {
	project := agentPluginTestProject(t)
	source := filepath.Join(t.TempDir(), "source")
	writeAgentPluginFixture(t, source, validAgentPluginManifest("portable"))
	destination := filepath.Join(project, "agent-plugins", "portable")
	if err := os.MkdirAll(destination, 0o755); err != nil {
		t.Fatal(err)
	}
	stale := filepath.Join(destination, "stale.txt")
	if err := os.WriteFile(stale, []byte("keep"), 0o644); err != nil {
		t.Fatal(err)
	}

	_, _, err := executeForTest(
		t, defaultSystem(), "agent-plugins", "install", source, "--project", project,
	)
	if err == nil || !strings.Contains(err.Error(), "--force") {
		t.Fatalf("replacement error = %v", err)
	}
	if string(mustReadTestFile(t, stale)) != "keep" {
		t.Fatal("refused install changed the existing package")
	}
	if _, _, err := executeForTest(
		t, defaultSystem(), "agent-plugins", "install", source, "--project", project, "--force",
	); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatalf("forced install retained stale content: %v", err)
	}
}

func TestAgentPluginInstallValidatesManifestBeforeMutation(t *testing.T) {
	tests := map[string]string{
		"schema":    `{"$schema":"https://example.com/plugin.json","name":"portable"}`,
		"name":      `{"$schema":"https://agent-plugins.org/schemas/1.0.0/plugin.schema.json","name":"../escape"}`,
		"metadata":  `{"$schema":"https://agent-plugins.org/schemas/1.0.0/plugin.schema.json","name":"portable","keywords":[1]}`,
		"duplicate": `{"$schema":"https://agent-plugins.org/schemas/1.0.0/plugin.schema.json","name":"portable","name":"other"}`,
	}
	for name, manifest := range tests {
		t.Run(name, func(t *testing.T) {
			project := agentPluginTestProject(t)
			source := filepath.Join(t.TempDir(), "source")
			writeAgentPluginFixture(t, source, manifest)
			_, _, err := executeForTest(
				t, defaultSystem(), "agent-plugins", "install", source, "--project", project,
			)
			if err == nil || !strings.Contains(err.Error(), "plugin.json") {
				t.Fatalf("manifest validation error = %v", err)
			}
			entries, readErr := os.ReadDir(filepath.Join(project, "agent-plugins"))
			if readErr != nil || len(entries) != 0 {
				t.Fatalf("invalid package mutated destination: %v, %v", entries, readErr)
			}
		})
	}
}

func TestAgentPluginInstallRejectsSourceSymlinks(t *testing.T) {
	project := agentPluginTestProject(t)
	source := filepath.Join(t.TempDir(), "source")
	writeAgentPluginFixture(t, source, validAgentPluginManifest("portable"))
	outside := filepath.Join(t.TempDir(), "outside")
	if err := os.WriteFile(outside, []byte("secret"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(source, "linked")); err != nil {
		t.Fatal(err)
	}
	_, _, err := executeForTest(
		t, defaultSystem(), "agent-plugins", "install", source, "--project", project,
	)
	if err == nil || !strings.Contains(err.Error(), "cannot be symlinks") {
		t.Fatalf("source symlink error = %v", err)
	}
	assertAgentPluginDirectoryEmpty(t, filepath.Join(project, "agent-plugins"))
}

func TestAgentPluginInstallRejectsDestinationSymlink(t *testing.T) {
	project := agentPluginTestProject(t)
	source := filepath.Join(t.TempDir(), "source")
	writeAgentPluginFixture(t, source, validAgentPluginManifest("portable"))
	if err := os.Remove(filepath.Join(project, "agent-plugins")); err != nil {
		t.Fatal(err)
	}
	outsideDirectory := t.TempDir()
	if err := os.Symlink(outsideDirectory, filepath.Join(project, "agent-plugins")); err != nil {
		t.Fatal(err)
	}
	_, _, err := executeForTest(
		t, defaultSystem(), "agent-plugins", "install", source, "--project", project,
	)
	if err == nil || !strings.Contains(err.Error(), "target agent-plugins directory cannot be a symlink") {
		t.Fatalf("destination symlink error = %v", err)
	}
	assertAgentPluginDirectoryEmpty(t, outsideDirectory)
}

// TestAgentPluginInstallRejectsLegacyPluginsDirectory prevents dual plugins/ and agent-plugins/ state.
func TestAgentPluginInstallRejectsLegacyPluginsDirectory(t *testing.T) {
	project := agentPluginTestProject(t)
	if err := os.Remove(filepath.Join(project, "agent-plugins")); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(project, "plugins"), 0o755); err != nil {
		t.Fatal(err)
	}
	source := filepath.Join(t.TempDir(), "source")
	writeAgentPluginFixture(t, source, validAgentPluginManifest("portable"))
	_, _, err := executeForTest(
		t, defaultSystem(), "agent-plugins", "install", source, "--project", project,
	)
	// Require harnest upgrade before creating agent-plugins/ alongside legacy plugins/.
	if err == nil || !strings.Contains(err.Error(), "harnest upgrade") {
		t.Fatalf("legacy plugins error = %v", err)
	}
	if _, statErr := os.Stat(filepath.Join(project, "agent-plugins")); !os.IsNotExist(statErr) {
		t.Fatalf("refused install created agent-plugins/: %v", statErr)
	}
}

func assertAgentPluginDirectoryEmpty(t *testing.T, directory string) {
	t.Helper()
	entries, readErr := os.ReadDir(directory)
	if readErr != nil || len(entries) != 0 {
		t.Fatalf("plugin directory was changed: %v, %v", entries, readErr)
	}
}

// TestAgentPluginAndExtensionCommandsAreDistinct verifies agent-plugins replaced plugins and stays separate from extensions.
func TestAgentPluginAndExtensionCommandsAreDistinct(t *testing.T) {
	pluginHelp, _, err := executeForTest(t, defaultSystem(), "agent-plugins", "--help")
	if err != nil {
		t.Fatal(err)
	}
	if _, _, legacyErr := executeForTest(t, defaultSystem(), "plugins", "--help"); legacyErr == nil {
		t.Fatal("retired 'plugins' command unexpectedly succeeded")
	}
	extensionHelp, _, err := executeForTest(t, defaultSystem(), "extensions", "--help")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(pluginHelp, "Install portable Agent Plugins") || !strings.Contains(pluginHelp, "install") || strings.Contains(pluginHelp, "Search Harnest Extension") {
		t.Fatalf("unexpected agent-plugins help:\n%s", pluginHelp)
	}
	if !strings.Contains(extensionHelp, "Discover Harnest Extensions") || !strings.Contains(extensionHelp, "search") || strings.Contains(extensionHelp, "Install portable") {
		t.Fatalf("unexpected extensions help:\n%s", extensionHelp)
	}
}

// agentPluginTestProject creates a minimal project root with the canonical agent-plugins/ directory.
func agentPluginTestProject(t *testing.T) string {
	t.Helper()
	project := t.TempDir()
	if err := os.WriteFile(filepath.Join(project, "config.yaml"), []byte("kind: Agent\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(project, "agent-plugins"), 0o755); err != nil {
		t.Fatal(err)
	}
	return project
}

func writeAgentPluginFixture(t *testing.T, root, manifest string) {
	t.Helper()
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "plugin.json"), []byte(manifest), 0o644); err != nil {
		t.Fatal(err)
	}
}

func validAgentPluginManifest(name string) string {
	return `{"$schema":"https://agent-plugins.org/schemas/1.0.0/plugin.schema.json","name":"` + name + `"}`
}
