package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"harnest.dev/harnest/engine"
)

func TestDependencyPolicyRejectsCompilerOwnedFrameworkPackages(t *testing.T) {
	tests := map[string][]string{
		"adk": {
			`dependencies = ["google_adk[eval]>=99"]`,
			`dependencies = ["harnest[langgraph]>=99"]`,
			`dependencies = ["langgraph>=99"]`,
			"[project.optional-dependencies]\nframework = [\"google-adk @ https://example.test/adk.whl\"]",
			"[tool.uv]\noverride-dependencies = [\"google.adk==99\"]",
		},
		"langgraph": {
			`dependencies = ["LangGraph>=99"]`,
			"[dependency-groups]\ndev = [\"langchain-litellm>=99\"]",
			"[tool.uv]\nconstraint-dependencies = [\"langchain_mcp_adapters==99\"]",
		},
	}
	for framework, declarations := range tests {
		for index, declaration := range declarations {
			t.Run(fmt.Sprintf("%s-%d", framework, index), func(t *testing.T) {
				bundle := dependencyPolicyBundle(t, framework, declaration)
				err := validateAgentDependencyPolicy(bundle)
				if err == nil || !strings.Contains(err.Error(), "compiler-owned framework package") {
					t.Fatalf("expected framework ownership error, got %v", err)
				}
			})
		}
	}
}

func TestDependencyPolicyAllowsAgentOwnedPackages(t *testing.T) {
	bundle := dependencyPolicyBundle(
		t,
		"adk",
		"dependencies = [\"httpx>=0.28,<1\"]\n[dependency-groups]\ndev = [\"pytest>=8,<9\"]",
	)
	if err := validateAgentDependencyPolicy(bundle); err != nil {
		t.Fatal(err)
	}
}

func TestDependencyPolicyIgnoresDependencyGroupNames(t *testing.T) {
	bundle := dependencyPolicyBundle(
		t,
		"adk",
		"dependencies = []\n[dependency-groups]\ndev = [{include-group = \"langgraph\"}]",
	)
	if err := validateAgentDependencyPolicy(bundle); err != nil {
		t.Fatal(err)
	}
}

func TestEnvironmentSyncRejectsFrameworkOverrideBeforeUV(t *testing.T) {
	root := t.TempDir()
	agent := filepath.Join(root, "owned-agent")
	if err := createScaffold(agent, "owned-agent"); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(agent, "pyproject.toml")
	contents := strings.Replace(
		string(mustReadTestFile(t, path)),
		"dependencies = []",
		`dependencies = ["google-adk==99"]`,
		1,
	)
	if err := os.WriteFile(path, []byte(contents), 0o644); err != nil {
		t.Fatal(err)
	}
	calls := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	_, _, err := executeForTest(t, environmentTestSystem(t, root), "env", "sync", agent)
	if err == nil || !strings.Contains(err.Error(), "compiler-owned framework package") {
		t.Fatalf("expected framework ownership error, got %v", err)
	}
	if _, statErr := os.Stat(calls); !os.IsNotExist(statErr) {
		t.Fatalf("uv ran before dependency policy validation: %v", statErr)
	}
}

func dependencyPolicyBundle(t *testing.T, framework, declaration string) engine.Bundle {
	t.Helper()
	root := filepath.Join(t.TempDir(), "agent")
	if err := createScaffoldForFramework(root, "agent", framework); err != nil {
		t.Fatal(err)
	}
	contents := "[project]\nname = \"agent\"\nversion = \"0.1.0\"\n" + declaration + "\n"
	if err := os.WriteFile(filepath.Join(root, "pyproject.toml"), []byte(contents), 0o644); err != nil {
		t.Fatal(err)
	}
	bundle, err := loadAgentBundle(root)
	if err != nil {
		t.Fatal(err)
	}
	return bundle
}
