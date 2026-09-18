package main

import (
	"archive/zip"
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writePackageFixture(t *testing.T, root string) {
	t.Helper()
	files := map[string]string{
		"config.yaml":     "apiVersion: harnest.dev/v1alpha1\nkind: Agent\nmetadata:\n  name: support-agent\n  displayName: Support Agent\nspec:\n  entrypoint: agent:root_agent\n  framework:\n    name: adk\n    mode: managed\n  runtime:\n    version: \"3.12\"\n    dependencyFile: pyproject.toml\n",
		"agent.py":        "from harnest.agent import Agent\n\nroot_agent = Agent(name=\"support_agent\")\n",
		"agent-card.yaml": "name: Support Agent\ndescription: A support template.\nversion: 0.1.0\n",
		"pyproject.toml":  "[project]\nname = \"support-agent\"\n",
		"instructions.md": "# Support agent\n",
		"tools/echo.py":   "def echo(message):\n    return message\n",
		".gitignore":      ".harnest/\n.venv\n",
	}
	for name, contents := range files {
		path := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(contents), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	// Build and cache state must never enter the wheel.
	for _, ignored := range []string{".harnest/artifacts/x", ".venv/lib", "dist/old.whl", "__pycache__/agent.pyc"} {
		path := filepath.Join(root, ignored)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte("ignored"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func wheelEntryNames(t *testing.T, contents []byte) map[string]bool {
	t.Helper()
	reader, err := zip.NewReader(bytes.NewReader(contents), int64(len(contents)))
	if err != nil {
		t.Fatal(err)
	}
	names := map[string]bool{}
	for _, file := range reader.File {
		names[file.Name] = true
	}
	return names
}

func TestPackageTemplateBuildsWheelAndRoundTrips(t *testing.T) {
	root := t.TempDir()
	writePackageFixture(t, root)
	output := filepath.Join(root, "dist", "support.whl")

	app := testTemplateApplication()
	result, err := app.packageTemplate(root, "", "0.1.0", output)
	if err != nil {
		t.Fatal(err)
	}
	if result.Slug != "support-agent" {
		t.Fatalf("slug = %q, want support-agent", result.Slug)
	}
	wheel, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	assertPackageWheelEntries(t, wheel)
	assertPackageRoundTrip(t, wheel)
}

func assertPackageWheelEntries(t *testing.T, wheel []byte) {
	t.Helper()
	names := wheelEntryNames(t, wheel)
	module := "harnest_template_support_agent"
	distInfo := "harnest_template_support_agent-0.1.0.dist-info"
	for _, expected := range []string{
		module + "/harnest-template.yaml",
		module + "/template/config.yaml",
		module + "/template/agent.py",
		module + "/template/agent-card.yaml",
		module + "/template/pyproject.toml",
		module + "/template/instructions.md",
		module + "/template/tools/echo.py",
		module + "/template/.gitignore",
		distInfo + "/METADATA",
		distInfo + "/WHEEL",
		distInfo + "/entry_points.txt",
	} {
		if !names[expected] {
			t.Fatalf("wheel is missing %s", expected)
		}
	}
	for name := range names {
		if strings.Contains(name, ".harnest/") || strings.Contains(name, ".venv/") ||
			strings.Contains(name, "__pycache__/") || strings.Contains(name, "/dist/") {
			t.Fatalf("wheel includes excluded state: %s", name)
		}
	}
}

func assertPackageRoundTrip(t *testing.T, wheel []byte) {
	t.Helper()
	pkg, err := readTemplateWheelPackage(wheel)
	if err != nil {
		t.Fatal(err)
	}
	if pkg.Slug != "support-agent" || pkg.Version != "0.1.0" {
		t.Fatalf("round-tripped identity = %q %q", pkg.Slug, pkg.Version)
	}
	if _, ok := pkg.Resources["config.yaml"]; !ok {
		t.Fatal("round-tripped resources missing config.yaml")
	}
}

func TestPackageTemplateDerivesSlugFromConfig(t *testing.T) {
	root := t.TempDir()
	writePackageFixture(t, root)
	app := testTemplateApplication()
	result, err := app.packageTemplate(root, "", "1.2.3", filepath.Join(root, "out.whl"))
	if err != nil {
		t.Fatal(err)
	}
	if result.Slug != "support-agent" {
		t.Fatalf("slug = %q", result.Slug)
	}
}

func TestPackageTemplateRejectsSymlink(t *testing.T) {
	root := t.TempDir()
	writePackageFixture(t, root)
	if err := os.Symlink("agent.py", filepath.Join(root, "linked.py")); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	app := testTemplateApplication()
	if _, err := app.packageTemplate(root, "", "0.1.0", filepath.Join(root, "out.whl")); err == nil ||
		!strings.Contains(err.Error(), "symlink") {
		t.Fatalf("symlink packaging error = %v", err)
	}
}

func TestPackageTemplateRequiresAgentConfig(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "agent.py"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	app := testTemplateApplication()
	if _, err := app.packageTemplate(root, "", "0.1.0", filepath.Join(root, "out.whl")); err == nil {
		t.Fatal("packaging accepted a directory without config.yaml")
	}
}
