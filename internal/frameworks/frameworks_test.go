package frameworks

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

// The Python compiler owns the canonical file; the Go copy must match it
// byte-for-byte so both halves of a release agree on every framework.
func TestEmbeddedMetadataMatchesPythonSource(t *testing.T) {
	canonical, err := os.ReadFile(filepath.Join("..", "..", "src", "harnest", "frameworks", "frameworks.json"))
	if err != nil {
		t.Fatalf("read canonical frameworks.json: %v", err)
	}
	if !bytes.Equal(canonical, embedded) {
		t.Fatal("internal/frameworks/frameworks.json differs from src/harnest/frameworks/frameworks.json; copy the canonical file")
	}
}

func TestRegistryExposesBothBuiltInFrameworks(t *testing.T) {
	if got := Choices(); got != "adk or langgraph" {
		t.Fatalf("Choices() = %q", got)
	}
	adk, ok := Lookup("adk")
	if !ok || adk.Distribution != "google-adk" || !adk.Supports(RootAgentSubagents) {
		t.Fatalf("unexpected adk metadata: %+v", adk)
	}
	langgraph, ok := Lookup("langgraph")
	if !ok || langgraph.Supports(RootAgentSubagents) || !langgraph.Supports(Evals) {
		t.Fatalf("unexpected langgraph metadata: %+v", langgraph)
	}
	if Known("unknown") {
		t.Fatal("unknown framework must not be registered")
	}
}

func TestRuntimeRequirementsAppendSharedStoreDrivers(t *testing.T) {
	adk, _ := Lookup("adk")
	got := adk.RuntimeRequirements()
	want := []string{"google-adk>=2.8,<3", "asyncpg>=0.30,<1", "redis>=6,<8"}
	if len(got) != len(want) {
		t.Fatalf("RuntimeRequirements() = %v", got)
	}
	for index := range want {
		if got[index] != want[index] {
			t.Fatalf("RuntimeRequirements() = %v, want %v", got, want)
		}
	}
}
