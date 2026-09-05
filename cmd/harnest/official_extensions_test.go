package main

import (
	"os"
	"path/filepath"
	"slices"
	"testing"
)

func TestOfficialExtensionsJoinRuntimeDependencyPlan(t *testing.T) {
	// Exercise the same filesystem-only collection used by env sync so the
	// official catalog cannot drift from installable extension metadata.
	extensions := filepath.Join("..", "..", "official-extensions")
	requirements, projects, err := packageRuntimeRequirements(extensions, true)
	if err != nil {
		t.Fatalf("inspect bundled extensions: %v", err)
	}
	for _, requirement := range []string{"docker>=7.1,<8", "hatchet-sdk>=1.38,<2"} {
		if !slices.Contains(requirements, requirement) {
			t.Errorf("runtime requirements %v do not contain %s", requirements, requirement)
		}
	}
	for _, name := range []string{"docker", "hatchet"} {
		expected := filepath.Join(extensions, name, "pyproject.toml")
		if !slices.Contains(projects, expected) {
			t.Errorf("runtime projects %v do not contain %s", projects, expected)
		}
	}
}

// TestOfficialExtensionsInstallFromCheckout keeps the published examples aligned
// with the same closed local-package layout enforced for third-party authors.
func TestOfficialExtensionsInstallFromCheckout(t *testing.T) {
	for _, name := range []string{"docker", "hatchet"} {
		t.Run(name, func(t *testing.T) {
			project := extensionTestProject(t)
			source := filepath.Join("..", "..", "official-extensions", name)
			if _, _, err := executeForTest(
				t, defaultSystem(), "extensions", "install", source, "--project", project,
			); err != nil {
				t.Fatalf("install official %s extension: %v", name, err)
			}
			destination := filepath.Join(project, "extensions", name)
			for _, required := range []string{"README.md", "extension.py", "extension.yaml", "pyproject.toml"} {
				if info, err := os.Stat(filepath.Join(destination, required)); err != nil || !info.Mode().IsRegular() {
					t.Errorf("installed %s/%s is not a regular file: %v", name, required, err)
				}
			}
		})
	}
}
