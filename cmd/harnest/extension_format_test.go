package main

import (
	"archive/zip"
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestExtensionWheelUsesCanonicalIdentity verifies static packaging without executing Python.
func TestExtensionWheelUsesCanonicalIdentity(t *testing.T) {
	var buffer bytes.Buffer
	writer := zip.NewWriter(&buffer)
	files := map[string]string{
		"clock-1.0.0.dist-info/entry_points.txt": "[harnest.extensions]\nclock = clock.extension:extension\n",
		"clock/extension.yaml":                   "apiVersion: harnest.dev/v1alpha1\nkind: Extension\nmetadata:\n  name: clock\n  version: 1.0.0\nruntime:\n  entrypoint: extension:extension\n",
		"clock/extension.py":                     "raise AssertionError('search must not execute this')\n",
	}
	for name, source := range files {
		file, err := writer.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = file.Write([]byte(source)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	if err := inspectPluginWheel(buffer.Bytes(), "harnest-extension-clock", "1.0.0"); err != nil {
		t.Fatal(err)
	}
	if err := inspectPluginWheel(buffer.Bytes(), "harnest-extension-clock", "2.0.0"); err == nil {
		t.Fatal("release identity mismatch accepted")
	}
}

// TestExtensionSearchCommandPreservesLegacyCatalog proves the command alias is functional.
func TestExtensionSearchCommandPreservesLegacyCatalog(t *testing.T) {
	var catalogs, metadata, wheels int
	sys := pluginSearchTestSystem(pluginCatalogFixture(t, &catalogs, &metadata, &wheels), t.TempDir())
	output, _, err := executeForTest(t, sys, "extensions", "search", "postgres")
	if err != nil || !strings.Contains(output, "Harnest_Plugin_Postgres") {
		t.Fatalf("search: %v %s", err, output)
	}
}

// TestExtensionDependenciesJoinRootEnvironment keeps SDK dependencies in the existing solve.
func TestExtensionDependenciesJoinRootEnvironment(t *testing.T) {
	root := t.TempDir()
	project := filepath.Join(root, "extensions", "clock", "pyproject.toml")
	if err := os.MkdirAll(filepath.Dir(project), 0o755); err != nil {
		t.Fatal(err)
	}
	mustWriteEnvironmentFixture(t, filepath.Join(root, "extensions", "clock", "extension.yaml"), "kind: Extension\n")
	mustWriteEnvironmentFixture(t, project, "[project]\nname = 'clock'\nversion = '1.0.0'\ndependencies = ['httpx>=0.28']\n")
	values, files, err := pluginRuntimeRequirements(root)
	if err != nil || len(files) != 1 || files[0] != project || len(values) != 1 || values[0] != "httpx>=0.28" {
		t.Fatalf("extension dependency solve: %v %v %v", values, files, err)
	}
}
