package main

import (
	"archive/zip"
	"context"
	"debug/elf"
	"encoding/binary"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"harnest.dev/harnest/internal/agentpack"
)

// compiledFixture uses the real Harnest runtime manifest format and a minimal inspectable amd64 ELF.
func compiledFixture(t *testing.T, root string) string {
	t.Helper()
	file, err := os.Create(filepath.Join(root, "compiled"))
	if err != nil {
		t.Fatal(err)
	}
	header := elf.Header64{Machine: uint16(elf.EM_X86_64), Version: 1, Ehsize: 64}
	copy(header.Ident[:], "\x7fELF\x02\x01\x01")
	if err := binary.Write(file, binary.LittleEndian, header); err != nil {
		t.Fatal(err)
	}
	archive := zip.NewWriter(file)
	manifest := agentpack.Manifest{Format: agentpack.Format, OS: "linux", Arch: "amd64", Python: "bin/python", Files: []agentpack.File{{Path: "bin/python", Object: "0000000000000000000000000000000000000000000000000000000000000000-555"}}}
	manifest.Seal()
	addFixtureConfig(t, archive, root)
	values := map[string]any{"runtime/runtime-manifest.json": manifest, "executable.json": agentpack.Executable{Format: "harnest.executable/v1", Embedded: true, Runtime: agentpack.Reference{OS: "linux", Arch: "amd64", Digest: manifest.Digest}}}
	for name, value := range values {
		stream, err := archive.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if err := json.NewEncoder(stream).Encode(value); err != nil {
			t.Fatal(err)
		}
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	return file.Name()
}

// TestPrepareContext covers target checking, atomic failure and source exclusion.
func TestPrepareContext(t *testing.T) {
	root := t.TempDir()
	artifact := compiledFixture(t, root)
	contextDir := filepath.Join(root, "context")
	cfg := buildSettings{artifact: artifact, output: contextDir, base: "debian@sha256:a3bba7dc7dbcc1f37c2856c51fc9a7fac96608f50cb87b1b4e4d8b8c85ae988e", architecture: "arm64", port: 1907}
	if err := prepare(context.Background(), cfg); err == nil {
		t.Fatal("accepted wrong architecture")
	}
	if _, err := os.Stat(contextDir); !os.IsNotExist(err) {
		t.Fatal("published partial output")
	}
	cfg.architecture = "amd64"
	if err := prepare(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	files, err := os.ReadDir(contextDir)
	if err != nil || len(files) != 2 || files[0].Name() != "Dockerfile" || files[1].Name() != "agent" {
		t.Fatal(files, err)
	}
	if err := prepare(context.Background(), cfg); err == nil {
		t.Fatal("overwrote an existing context")
	}
}

// addFixtureConfig includes the authored config for project-hook tests that verify agent identity.
func addFixtureConfig(t *testing.T, archive *zip.Writer, root string) {
	t.Helper()
	if data, err := os.ReadFile(filepath.Join(root, "config.yaml")); err == nil {
		stream, err := archive.Create("agent/source/config.yaml")
		if err != nil {
			t.Fatal(err)
		}
		if _, err = stream.Write(data); err != nil {
			t.Fatal(err)
		}
	}
}
