// Tests stamping, detection, caching, and validation for standalone branded pack binaries.

package packbinary

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// stageSamplePayload writes a valid staged pack binary payload for unit testing.
func stageSamplePayload(t *testing.T, root string) Manifest {
	t.Helper()
	digest := strings.Repeat("a", 64)
	wheelDigest := strings.Repeat("b", 64)
	manifest := Manifest{
		Format:            Format,
		Name:              "acme",
		Version:           "1.2.0",
		Title:             "Acme agents",
		Description:       "Acme defaults",
		Command:           "acme",
		HasProjectHooks:   true,
		Commands:          []CommandEntry{{Name: "whoami", Help: "Print agent team"}},
		Skills:            []SkillEntry{{Name: "acme-standards", Root: "skills/acme-standards"}},
		Wheels:            []WheelEntry{{Name: "acme-internal", Filename: "wheels/acme_internal-1.0.0-py3-none-any.whl", SHA256: wheelDigest}},
		Requirements:      []string{"acme-sdk==2.1.0"},
		ProtectedPackages: []string{"acme-internal", "acme-sdk"},
		Extensions:        []ExtensionEntry{{Name: "acme-guard", Path: "extensions/acme-guard", HasPyproject: true}},
		Digest:            digest,
	}
	files := map[string]string{
		"pack/pack.yaml":                              "apiVersion: harnest.dev/pack/v1\nname: acme\nversion: 1.2.0\ntitle: Acme agents\n",
		"skills/acme-standards/SKILL.md":              "---\nname: acme-standards\ndescription: Acme coding standards.\n---\n# Acme\n",
		"wheels/acme_internal-1.0.0-py3-none-any.whl": "synthetic-wheel-bytes",
		"extensions/acme-guard/extension.yaml":        "apiVersion: harnest.dev/v1alpha1\nkind: Extension\n",
		"extensions/acme-guard/pyproject.toml":        "[project]\nname = 'harnest-extension-acme-guard'\nversion = '1.0.0'\ndependencies = []\n",
	}
	for relative, content := range files {
		target := filepath.Join(root, filepath.FromSlash(relative))
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(target, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	encoded, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ManifestFile), encoded, 0o644); err != nil {
		t.Fatal(err)
	}
	return manifest
}

// assertMaterializedPack verifies extracted pack files and cache reuse.
func assertMaterializedPack(t *testing.T, output string) {
	t.Helper()
	cache := t.TempDir()
	loaded, err := Materialize(output, cache)
	if err != nil || loaded == nil {
		t.Fatalf("Materialize failed: loaded=%v err=%v", loaded, err)
	}
	if _, err := os.Stat(filepath.Join(loaded.PackDir(), "pack.yaml")); err != nil {
		t.Fatalf("missing materialized pack.yaml: %v", err)
	}
	if projects := loaded.ExtensionProjectFiles(); len(projects) != 1 {
		t.Fatalf("expected 1 extension pyproject.toml, got %v", projects)
	}
	// A second call must reuse the existing content-addressed cache cleanly.
	again, err := Materialize(output, cache)
	if err != nil || again == nil || again.Root != loaded.Root {
		t.Fatalf("cache reuse mismatch: %v %v", again, err)
	}
}

// TestWriteReadAndMaterializeRoundTrip stamps a binary, reads its manifest, and reuses the cache.
func TestWriteReadAndMaterializeRoundTrip(t *testing.T) {
	staged := filepath.Join(t.TempDir(), "staged")
	expected := stageSamplePayload(t, staged)
	output := filepath.Join(t.TempDir(), "dist", "acme")

	written, err := WriteBinary(output, []byte("#!/bin/sh\necho stub\n"), staged)
	if err != nil {
		t.Fatalf("WriteBinary failed: %v", err)
	}
	if written.Digest != expected.Digest || written.Command != "acme" {
		t.Fatalf("unexpected written manifest: %+v", written)
	}
	readBack, ok, err := ReadManifest(output)
	if err != nil || !ok {
		t.Fatalf("ReadManifest failed: ok=%v err=%v", ok, err)
	}
	if readBack.Name != "acme" || len(readBack.ProtectedPackages) != 2 {
		t.Fatalf("unexpected read manifest: %+v", readBack)
	}
	assertMaterializedPack(t, output)
}

// TestPlainBinaryHasNoEmbeddedPack ensures non-pack binaries return no embedded manifest.
func TestPlainBinaryHasNoEmbeddedPack(t *testing.T) {
	plain := filepath.Join(t.TempDir(), "harnest")
	if err := os.WriteFile(plain, []byte("#!/bin/sh\necho plain\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, ok, err := ReadManifest(plain); err != nil || ok {
		t.Fatalf("plain binary reported embedded pack: ok=%v err=%v", ok, err)
	}
	if loaded, err := Materialize(plain, t.TempDir()); err != nil || loaded != nil {
		t.Fatalf("plain binary materialized unexpected pack: %v %v", loaded, err)
	}
	if !IsWindowsPE([]byte("MZ-windows-stub")) || IsWindowsPE([]byte("#!/bin/sh")) {
		t.Fatal("IsWindowsPE header detection failed")
	}
}

// TestDoubleStampGuard blocks stamping a second pack onto an already stamped binary.
func TestDoubleStampGuard(t *testing.T) {
	staged := filepath.Join(t.TempDir(), "staged")
	stageSamplePayload(t, staged)
	stamped := filepath.Join(t.TempDir(), "acme")
	if _, err := WriteBinary(stamped, []byte("stub"), staged); err != nil {
		t.Fatal(err)
	}
	stampedBytes, err := os.ReadFile(stamped)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := WriteBinary(filepath.Join(t.TempDir(), "double"), stampedBytes, staged); err == nil {
		t.Fatal("accepted double-stamping an already stamped pack binary")
	}
}
