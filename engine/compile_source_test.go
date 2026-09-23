package engine

import (
	"path/filepath"
	"strings"
	"testing"
)

// TestCompiledSourceAllowsProjectOnlyDocuments verifies omitted guides without weakening retained-byte checks.
func TestCompiledSourceAllowsProjectOnlyDocuments(t *testing.T) {
	source, directory := compiledServerArtifactFixture(t)
	mustWrite(t, filepath.Join(source.Directory, "docs", "team-guide.md"), "For teammates only.\n")
	var err error
	source, err = LoadBundle(source.Directory)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := loadCompiledArtifact(directory, source); err != nil {
		t.Fatalf("project-only guide prevented compilation: %v", err)
	}
}

// TestCompiledSourceRejectsChangedOrInventedRecords checks source provenance even for self-consistent artifact hashes.
func TestCompiledSourceRejectsChangedOrInventedRecords(t *testing.T) {
	for _, name := range []string{"instructions.md", "invented.json", "../outside.txt"} {
		t.Run(name, func(t *testing.T) {
			source, directory := compiledServerArtifactFixture(t)
			manifest := readCompiledManifest(t, filepath.Join(directory, compiledManifestFilename))
			manifest.Files = []CompiledFile{{Path: "source/" + name, SHA256: strings.Repeat("0", 64), Size: 0}}
			if err := validateCompiledSource(directory, source, manifest); err == nil {
				t.Fatal("accepted a retained file without matching authored bytes")
			}
		})
	}
}

// TestCompiledSourceRejectsStaleSnapshot catches edits during compilation even to files omitted from the artifact.
func TestCompiledSourceRejectsStaleSnapshot(t *testing.T) {
	source, directory := compiledServerArtifactFixture(t)
	mustWrite(t, filepath.Join(source.Directory, "team-guide.md"), "Changed while compiling.\n")
	if _, err := loadCompiledArtifact(directory, source); err == nil || !strings.Contains(err.Error(), "changed during compilation") {
		t.Fatalf("expected stale source rejection, got %v", err)
	}
}
