package main

import (
	"os"
	"path/filepath"
	"testing"
)

// TestSubagentDependencyDiscovery covers both compiler-supported authoring layouts.
func TestSubagentDependencyDiscovery(t *testing.T) {
	root := t.TempDir()
	subagents := filepath.Join(root, "subagents")
	if err := os.MkdirAll(subagents, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(subagents, "helper.py"), []byte("raise RuntimeError('never import')"), 0o644); err != nil {
		t.Fatal(err)
	}
	found, err := hasAuthoredMCP(root)
	if err != nil || found {
		t.Fatalf("flat agent: found=%v err=%v", found, err)
	}
	mcp := filepath.Join(subagents, "nested", "mcp")
	if err := os.MkdirAll(mcp, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(mcp, "service.py"), []byte("pass"), 0o644); err != nil {
		t.Fatal(err)
	}
	found, err = hasAuthoredMCP(root)
	if err != nil || !found {
		t.Fatalf("nested agent: found=%v err=%v", found, err)
	}
}

// TestSubagentDependencyDiscoveryRejectsLinks retains the dependency walk's ownership boundary.
func TestSubagentDependencyDiscoveryRejectsLinks(t *testing.T) {
	root := t.TempDir()
	subagents := filepath.Join(root, "subagents")
	if err := os.MkdirAll(subagents, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(t.TempDir(), filepath.Join(subagents, "linked.py")); err != nil {
		t.Fatal(err)
	}
	if _, err := hasAuthoredMCP(root); err == nil {
		t.Fatal("accepted linked subagent")
	}
}
