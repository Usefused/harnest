package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
	"harnest.dev/harnest/engine"
)

// TestInitProposal proves Hostlier can preview and install the same loadable starter using inline inputs.
func TestInitProposal(t *testing.T) {
	root := t.TempDir()
	t.Chdir(root)
	req := initRequest{Version: 1, DefaultName: "unused", Port: 9042, Route: "starter.example.com", Architecture: "amd64", Config: map[string]any{"name": "starter-agent", "baseImage": "debian@sha256:" + strings.Repeat("a", 64), "requiredBindings": []string{"CACHE_URL"}}}
	raw, err := json.Marshal(req)
	if err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err = run(context.Background(), []string{"init"}, bytes.NewReader(raw), &out); err != nil {
		t.Fatal(err)
	}
	var result initResponse
	if err = json.Unmarshal(out.Bytes(), &result); err != nil || result.Error != "" || result.Project["name"] != "starter-agent" {
		t.Fatal(result.Error, err)
	}
	installInitProposal(t, root, result.Files)
	bundle, err := engine.LoadBundle(root)
	if err != nil || bundle.Config.Metadata.Name != "starter-agent" || !strings.Contains(result.Files["agent-card.yaml"], ":9042") {
		t.Fatal(err)
	}
	checkStarterPackaging(t, root, req.Config, result.Project)

}

// installInitProposal checks dry-run isolation and then applies the proposed files just as Hostlier would.
func installInitProposal(t *testing.T, root string, files map[string]string) {
	t.Helper()
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) != 0 {
		t.Fatal("init hook wrote files", err)
	}
	for name, body := range files {
		path := filepath.Join(root, name)
		if err = os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(path, []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
	}
}

// TestInitRejectsConflictingNames prevents a command-line name from silently replacing configured identity.
func TestInitRejectsConflictingNames(t *testing.T) {
	_, err := proposeInit(initRequest{Name: "second", Config: map[string]any{"name": "first", "baseImage": "debian@sha256:" + strings.Repeat("a", 64)}})
	if err == nil || !strings.Contains(err.Error(), "--name") {
		t.Fatal(err)
	}
}

// TestInitWithOnlyName allows authoring the starter before choosing a deployment base image.
func TestInitWithOnlyName(t *testing.T) {
	response, err := proposeInit(initRequest{DefaultName: "fallback", Config: map[string]any{"name": "configured"}})
	if err != nil || response.Project["name"] != "configured" {
		t.Fatal(response.Error, err)
	}
}

// checkStarterPackaging exercises the plan and package hooks against the generated project, including its bindings.
func checkStarterPackaging(t *testing.T, root string, inputs, project map[string]any) {
	t.Helper()
	inputs["artifact"] = "compiled"
	project["packages"] = map[string]any{"harnest": inputs}
	project["env"] = map[string]string{"CACHE_URL": "fixture"}
	raw, err := yaml.Marshal(project)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(root, ".hostlier"), raw, 0600); err != nil {
		t.Fatal(err)
	}
	compiledFixture(t, root)
	req := hookRequest{Version: 1, Architecture: "amd64", ProjectDir: root, Config: inputs}
	plan := invokeHook(t, req, "project-plan")
	if plan.Error != "" || plan.Declarations == nil {
		t.Fatal(plan.Error)
	}
	req.Project, req.Declarations, req.OutputDir = plan.Project, plan.Declarations, t.TempDir()
	packaged := invokeHook(t, req, "project-package")
	if packaged.Error != "" || packaged.Artifact["kind"] != "container-context" {
		t.Fatal(packaged)
	}
}
