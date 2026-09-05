package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestExtensionInitCreatesCanonicalLocalPackage(t *testing.T) {
	project := extensionTestProject(t)
	stdout, _, err := executeForTest(
		t, defaultSystem(), "extensions", "init", "docker_provider",
		"--project", project, "--capability", "sandbox.provider",
	)
	if err != nil {
		t.Fatal(err)
	}
	destination := filepath.Join(project, "extensions", "docker_provider")
	assertContainsAll(t, "extension init", stdout, []string{"docker_provider", destination})
	assertContainsAll(t, "extension init next step", stdout, []string{"harnest env sync", project})
	assertFilesContain(t, destination, map[string]string{
		"extension.yaml": "  - sandbox.provider",
		"extension.py":   "class DockerProviderExtension(Extension)",
		"pyproject.toml": `name = "harnest-extension-docker-provider"`,
	})
	manifest, err := readLocalExtensionManifest(destination)
	if err != nil {
		t.Fatalf("generated extension is invalid: %v", err)
	}
	if manifest.Metadata.Name != "docker_provider" || manifest.Metadata.Version != "0.1.0" {
		t.Fatalf("generated manifest identity = %#v", manifest.Metadata)
	}
}

func TestExtensionInitValidatesBeforeMutationAndRefusesReplacement(t *testing.T) {
	for _, test := range []struct {
		name       string
		capability string
	}{
		{name: "bad-name"},
		{name: "class"},
		{name: "valid_name", capability: "host.root"},
	} {
		t.Run(test.name+test.capability, func(t *testing.T) {
			project := extensionTestProject(t)
			arguments := []string{"extensions", "init", test.name, "--project", project}
			if test.capability != "" {
				arguments = append(arguments, "--capability", test.capability)
			}
			if _, _, err := executeForTest(t, defaultSystem(), arguments...); err == nil {
				t.Fatal("invalid scaffold request was accepted")
			}
			assertExtensionDirectoryEmpty(t, filepath.Join(project, "extensions"))
		})
	}

	project := extensionTestProject(t)
	destination := filepath.Join(project, "extensions", "clock")
	if err := os.MkdirAll(destination, 0o755); err != nil {
		t.Fatal(err)
	}
	stale := filepath.Join(destination, "keep.txt")
	if err := os.WriteFile(stale, []byte("keep"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, _, err := executeForTest(
		t, defaultSystem(), "extensions", "init", "clock", "--project", project,
	)
	if err == nil || !strings.Contains(err.Error(), "already exists") {
		t.Fatalf("replacement error = %v", err)
	}
	if string(mustReadTestFile(t, stale)) != "keep" {
		t.Fatal("refused extension init changed the existing directory")
	}
}

func TestExtensionInstallCopiesCanonicalPackageByManifestIdentity(t *testing.T) {
	project := extensionTestProject(t)
	source := filepath.Join(t.TempDir(), "downloaded-folder")
	writeExtensionFixture(t, source, validExtensionManifest("docker_provider"))
	if err := os.WriteFile(filepath.Join(source, "README.md"), []byte("# Docker provider\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	script := filepath.Join(source, "lib", "helper.py")
	if err := os.MkdirAll(filepath.Dir(script), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(script, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	stdout, _, err := executeForTest(
		t, defaultSystem(), "extensions", "install", source, "--project", project,
	)
	if err != nil {
		t.Fatal(err)
	}
	destination := filepath.Join(project, "extensions", "docker_provider")
	assertContainsAll(t, "extension install", stdout, []string{"docker_provider", destination})
	assertContainsAll(t, "extension install next step", stdout, []string{"harnest env sync", project})
	info, err := os.Stat(filepath.Join(destination, "lib", "helper.py"))
	if err != nil || info.Mode().Perm()&0o111 == 0 {
		t.Fatalf("executable mode was not preserved: %v, %v", info, err)
	}
	if contents := string(mustReadTestFile(t, filepath.Join(destination, "README.md"))); contents != "# Docker provider\n" {
		t.Fatalf("installed README = %q", contents)
	}
}

func TestExtensionInstallValidatesLayoutAndOptionalProject(t *testing.T) {
	for name, mutate := range map[string]func(string){
		"unexpected root": func(source string) {
			if err := os.WriteFile(filepath.Join(source, "NOTES.md"), []byte("not compiled\n"), 0o644); err != nil {
				t.Fatal(err)
			}
		},
		"readme directory": func(source string) {
			if err := os.Mkdir(filepath.Join(source, "README.md"), 0o755); err != nil {
				t.Fatal(err)
			}
		},
		"project name": func(source string) {
			writeExtensionProject(t, source, "other", "0.1.0")
		},
		"project version": func(source string) {
			writeExtensionProject(t, source, "harnest-extension-clock", "2.0.0")
		},
	} {
		t.Run(name, func(t *testing.T) {
			project := extensionTestProject(t)
			source := filepath.Join(t.TempDir(), "source")
			writeExtensionFixture(t, source, validExtensionManifest("clock"))
			mutate(source)
			if _, _, err := executeForTest(
				t, defaultSystem(), "extensions", "install", source, "--project", project,
			); err == nil {
				t.Fatal("invalid extension layout was accepted")
			}
			assertExtensionDirectoryEmpty(t, filepath.Join(project, "extensions"))
		})
	}

	project := extensionTestProject(t)
	source := filepath.Join(t.TempDir(), "source")
	writeExtensionFixture(t, source, validExtensionManifest("clock"))
	writeExtensionProject(t, source, "harnest-extension-clock", "0.1.0")
	if _, _, err := executeForTest(
		t, defaultSystem(), "extensions", "install", source, "--project", project,
	); err != nil {
		t.Fatalf("canonical prefixed project identity was rejected: %v", err)
	}

	legacyProject := extensionTestProject(t)
	legacySource := filepath.Join(t.TempDir(), "legacy")
	writeExtensionFixture(t, legacySource, validExtensionManifest("clock"))
	writeExtensionProject(t, legacySource, "clock", "0.1.0")
	if _, _, err := executeForTest(
		t, defaultSystem(), "extensions", "install", legacySource, "--project", legacyProject,
	); err == nil || !strings.Contains(err.Error(), "harnest-extension-clock") {
		t.Fatalf("legacy project identity error = %v", err)
	}
}

func TestExtensionInstallRefusesReplacementUnlessForced(t *testing.T) {
	project := extensionTestProject(t)
	source := filepath.Join(t.TempDir(), "source")
	writeExtensionFixture(t, source, validExtensionManifest("clock"))
	destination := filepath.Join(project, "extensions", "clock")
	if err := os.MkdirAll(destination, 0o755); err != nil {
		t.Fatal(err)
	}
	stale := filepath.Join(destination, "stale.txt")
	if err := os.WriteFile(stale, []byte("keep"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, _, err := executeForTest(
		t, defaultSystem(), "extensions", "install", source, "--project", project,
	)
	if err == nil || !strings.Contains(err.Error(), "--force") {
		t.Fatalf("replacement error = %v", err)
	}
	if _, _, err := executeForTest(
		t, defaultSystem(), "extensions", "install", source,
		"--project", project, "--force",
	); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatalf("forced install retained stale content: %v", err)
	}
}

func TestExtensionInstallRejectsInvalidCanonicalPackageBeforeMutation(t *testing.T) {
	tests := map[string]struct {
		manifest string
		entry    bool
	}{
		"api version":     {manifest: strings.ReplaceAll(validExtensionManifest("clock"), "harnest.dev/v1alpha1", "example.dev/v1"), entry: true},
		"kind":            {manifest: strings.ReplaceAll(validExtensionManifest("clock"), "kind: Extension", "kind: RuntimePlugin"), entry: true},
		"name":            {manifest: strings.ReplaceAll(validExtensionManifest("clock"), "name: clock", "name: bad-name"), entry: true},
		"version":         {manifest: strings.ReplaceAll(validExtensionManifest("clock"), "0.1.0", "latest"), entry: true},
		"entrypoint":      {manifest: strings.ReplaceAll(validExtensionManifest("clock"), "extension:extension", "plugin:plugin"), entry: true},
		"capability":      {manifest: strings.ReplaceAll(validExtensionManifest("clock"), "capabilities: []", "capabilities: [host.root]"), entry: true},
		"unknown":         {manifest: validExtensionManifest("clock") + "unknown: true\n", entry: true},
		"documents":       {manifest: validExtensionManifest("clock") + "---\n{}\n", entry: true},
		"entrypoint file": {manifest: validExtensionManifest("clock"), entry: false},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			project := extensionTestProject(t)
			source := filepath.Join(t.TempDir(), "source")
			if err := os.MkdirAll(source, 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(source, "extension.yaml"), []byte(test.manifest), 0o644); err != nil {
				t.Fatal(err)
			}
			if test.entry {
				if err := os.WriteFile(filepath.Join(source, "extension.py"), []byte("extension = object()\n"), 0o644); err != nil {
					t.Fatal(err)
				}
			}
			if _, _, err := executeForTest(
				t, defaultSystem(), "extensions", "install", source, "--project", project,
			); err == nil {
				t.Fatal("invalid Harnest Extension was accepted")
			}
			assertExtensionDirectoryEmpty(t, filepath.Join(project, "extensions"))
		})
	}
}

func TestExtensionInstallRejectsSourceAndDestinationSymlinks(t *testing.T) {
	project := extensionTestProject(t)
	source := filepath.Join(t.TempDir(), "source")
	writeExtensionFixture(t, source, validExtensionManifest("clock"))
	outside := filepath.Join(t.TempDir(), "outside")
	if err := os.WriteFile(outside, []byte("secret"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(source, "README.md")); err != nil {
		t.Fatal(err)
	}
	_, _, err := executeForTest(
		t, defaultSystem(), "extensions", "install", source, "--project", project,
	)
	if err == nil || !strings.Contains(err.Error(), "cannot be symlinks") {
		t.Fatalf("source symlink error = %v", err)
	}

	if err := os.Remove(filepath.Join(project, "extensions")); err != nil {
		t.Fatal(err)
	}
	outsideDirectory := t.TempDir()
	if err := os.Symlink(outsideDirectory, filepath.Join(project, "extensions")); err != nil {
		t.Fatal(err)
	}
	cleanSource := filepath.Join(t.TempDir(), "clean")
	writeExtensionFixture(t, cleanSource, validExtensionManifest("clock"))
	_, _, err = executeForTest(
		t, defaultSystem(), "extensions", "install", cleanSource, "--project", project,
	)
	if err == nil || !strings.Contains(err.Error(), "target extensions directory cannot be a symlink") {
		t.Fatalf("destination symlink error = %v", err)
	}
	assertExtensionDirectoryEmpty(t, outsideDirectory)
}

func extensionTestProject(t *testing.T) string {
	t.Helper()
	project := t.TempDir()
	if err := os.WriteFile(filepath.Join(project, "config.yaml"), []byte("kind: Agent\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(project, "extensions"), 0o755); err != nil {
		t.Fatal(err)
	}
	return project
}

func writeExtensionFixture(t *testing.T, root, manifest string) {
	t.Helper()
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "extension.yaml"), []byte(manifest), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "extension.py"), []byte("extension = object()\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func writeExtensionProject(t *testing.T, root, name, version string) {
	t.Helper()
	contents := "[project]\nname = \"" + name + "\"\nversion = \"" + version + "\"\ndependencies = []\n"
	if err := os.WriteFile(filepath.Join(root, "pyproject.toml"), []byte(contents), 0o644); err != nil {
		t.Fatal(err)
	}
}

func validExtensionManifest(name string) string {
	return "apiVersion: harnest.dev/v1alpha1\n" +
		"kind: Extension\n" +
		"metadata:\n  name: " + name + "\n  version: 0.1.0\n" +
		"runtime:\n  entrypoint: extension:extension\n" +
		"capabilities: []\n"
}

func assertExtensionDirectoryEmpty(t *testing.T, directory string) {
	t.Helper()
	entries, err := os.ReadDir(directory)
	if err != nil || len(entries) != 0 {
		t.Fatalf("extensions directory was changed: %v, %v", entries, err)
	}
}
