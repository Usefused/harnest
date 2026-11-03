package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// copyAgentFixture copies only the declarations needed to plan an existing agent.
func copyAgentFixture(t *testing.T, root string) {
	t.Helper()
	source := filepath.Join("..", "..", "examples", "self-serve", "agents", "helpdesk")
	for _, name := range []string{"config.yaml", "agent-card.yaml", "instructions.md", "agent.py", "pyproject.toml"} {
		raw, err := os.ReadFile(filepath.Join(source, name))
		if err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(filepath.Join(root, name), raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
}

func TestProjectHooks(t *testing.T) {
	root := t.TempDir()
	copyAgentFixture(t, root)
	compiledFixture(t, root)
	const topology = `version: 2
package: harnest
packages:
  harnest:
    baseImage: debian@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    artifact: compiled
    requiredBindings: [CACHE_URL, SANDBOX_URL, SANDBOX_TOKEN]
  another: {setting: local}
name: support-production
endpoint: {port: 1907, exposure: public, route: support.example.com}
dependencies: {cache: {type: redis, source: provision, storage: 1Gi}}
bindings: {CACHE_URL: cache.url, SANDBOX_URL: applications.runner.url}
secrets: {SANDBOX_TOKEN: "secret://runner-token", KNOWLEDGE_MCP_TOKEN: "secret://knowledge-token"}
`
	set := func(name, body string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(root, name), []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
	}
	set(".hostlier", topology)
	request := hookRequest{Version: 1, ProjectDir: root, Architecture: "amd64"}

	plan := invokeHook(t, request, "project-plan")
	if plan.Project["packages"] != nil {
		t.Fatal("local settings reached the controller")
	}
	if plan.Error != "" {
		t.Fatal(plan.Error)
	}
	if got := plan.Project["secrets"].(map[string]any)["SANDBOX_TOKEN"]; got != "runner-token" {
		t.Fatal(got)
	}
	request.Project, request.Declarations, request.OutputDir = plan.Project, plan.Declarations, t.TempDir()
	if got := invokeHook(t, request, "project-package"); got.Artifact["kind"] != "container-context" {
		t.Fatal(got)
	}
	set("agent.py", "raise Exception('plan must never execute this source')\n")
	if got := invokeHook(t, request, "project-package"); !strings.Contains(got.Error, "changed") {
		t.Fatalf("accepted stale plan: %+v", got)
	}
	set(".hostlier", strings.Replace(topology, "secret://runner-token", "inline-sensitive-value", 1))
	if got := invokeHook(t, request, "project-plan"); strings.Contains(got.Error, "inline-sensitive-value") || got.Project != nil {
		t.Fatal("exposed inline secret")
	}
	set(".hostlier", strings.Replace(topology, "CACHE_URL: cache.url, ", "", 1))
	checkContributedBinding(t, request)
}

// checkContributedBinding requires an explicitly supplied resource binding before accepting the plan.
func checkContributedBinding(t *testing.T, request hookRequest) {
	t.Helper()
	if got := invokeHook(t, request, "project-plan"); !strings.Contains(got.Error, "CACHE_URL") {
		t.Fatal(got)
	}
	request.ProvidedBindings = []string{"CACHE_URL"}
	if got := invokeHook(t, request, "project-plan"); got.Error != "" {
		t.Fatal(got.Error)
	}
}

// invokeHook roundtrips the versioned public hook protocol over JSON pipes.
func invokeHook(t *testing.T, request hookRequest, action string) hookResponse {
	t.Helper()
	data, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	if err := run(context.Background(), []string{action}, bytes.NewReader(data), &output); err != nil {
		t.Fatal(err)
	}
	var result hookResponse
	if err := json.Unmarshal(output.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	return result
}

// TestLegacySettings preserves existing deployments and rejects misspelled inline settings without falling back.
func TestLegacySettings(t *testing.T) {
	root := t.TempDir()
	base := "debian@sha256:" + strings.Repeat("b", 64)
	err := os.WriteFile(filepath.Join(root, "harnest-hostlier.yaml"), []byte("version: 1\nbaseImage: "+base+"\n"), 0600)
	if err != nil {
		t.Fatal(err)
	}
	settings, err := readSettings(root, map[string]any{"packages": map[string]any{"harnest": map[string]any{}}})
	if err != nil || settings.BaseImage != base {
		t.Fatal(settings, err)
	}
	project := map[string]any{"packages": map[string]any{"harnest": map[string]any{"baseImage": base, "typo": true}}}
	if _, err = readSettings(root, project); err == nil {
		t.Fatal("invalid inline settings fell back to the legacy file")
	}
}
