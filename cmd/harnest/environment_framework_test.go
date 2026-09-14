package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFrameworkPinRequirement(t *testing.T) {
	pin := []byte("apiVersion: harnest.dev/v1alpha1\nkind: ProjectLock\nprojectSchema: 5\nframework:\n  name: langgraph\n  distribution: langgraph\n  version: 1.2.11\n")
	requirement, err := parseFrameworkRequirement(pin, "langgraph")
	if err != nil || requirement != "langgraph==1.2.11" {
		t.Fatalf("pin: %q %v", requirement, err)
	}
	requirement, err = parseFrameworkRequirement(pin, "adk")
	if err != nil || requirement != "" {
		t.Fatalf("switch: %q %v", requirement, err)
	}
	for _, bad := range []string{
		"framework: {name: langgraph, distribution: other, version: 1.2.11}",
		"framework: {name: langgraph, distribution: langgraph, version: '--index-url=other'}",
		"framework: {name: unknown, distribution: unknown, version: 1.0}",
	} {
		if _, err := parseFrameworkRequirement([]byte(bad), "langgraph"); err == nil {
			t.Fatalf("accepted %s", bad)
		}
	}
}

func TestEnvironmentSyncAppliesFrameworkPinAndRecordsInstalledVersion(t *testing.T) {
	root := t.TempDir()
	agent := filepath.Join(root, "pinned-agent")
	if err := createScaffold(agent, "pinned-agent"); err != nil {
		t.Fatal(err)
	}
	lockPath := filepath.Join(agent, "harnest.lock")
	mustWriteEnvironmentFixture(t, lockPath, string(mustReadTestFile(t, lockPath))+"framework:\n  name: adk\n  distribution: google-adk\n  version: 2.8.0\n")
	calls := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	sys := environmentTestSystem(t, root)
	mustSyncAgentEnvironment(t, sys, agent)
	if !strings.Contains(string(mustReadTestFile(t, calls)), "google-adk==2.8.0") {
		t.Fatal("runtime resolution input omitted the committed framework pin")
	}
	if err := os.Remove(filepath.Join(agent, ".harnest", environmentStateFile)); err != nil {
		t.Fatal(err)
	}
	mustWriteEnvironmentFixture(t, calls, "")
	mustSyncAgentEnvironment(t, sys, agent, "--frozen")
	assertContainsAll(t, "framework sync process calls", string(mustReadTestFile(t, calls)), []string{
		"pip sync --python",
		"pip check --python",
		"-m harnest.project_lock",
		"adk --frozen",
	})
	if strings.Contains(string(mustReadTestFile(t, calls)), "pip compile") {
		t.Fatal("frozen sync unexpectedly resolved dependencies")
	}
}

func TestFrameworkRequirementsAreReleaseOwned(t *testing.T) {
	tests := map[string]string{
		"adk": "google-adk>=2.8,<3\n" +
			"asyncpg>=0.30,<1\n" +
			"redis>=6,<8\n",
		"langgraph": "langgraph>=1.2,<2\n" +
			"langchain>=1.3,<2\n" +
			"langchain-litellm>=0.7,<1\n" +
			"asyncpg>=0.30,<1\n" +
			"redis>=6,<8\n",
	}
	for framework, expected := range tests {
		framework := framework
		expected := expected
		t.Run(framework, func(t *testing.T) {
			requirements, err := frameworkRequirements(framework)
			if err != nil {
				t.Fatal(err)
			}
			if requirements != expected {
				t.Fatalf("requirements = %q, want %q", requirements, expected)
			}
		})
	}
}

func TestScaffoldKeepsFrameworkRequirementsCompilerOwned(t *testing.T) {
	for _, framework := range []string{"adk", "langgraph"} {
		framework := framework
		t.Run(framework, func(t *testing.T) {
			directory := filepath.Join(t.TempDir(), framework+"-agent")
			if err := createScaffoldForFramework(directory, framework+"-agent", framework); err != nil {
				t.Fatal(err)
			}
			contents, err := os.ReadFile(filepath.Join(directory, "pyproject.toml"))
			if err != nil {
				t.Fatal(err)
			}
			expected, err := frameworkRequirements(framework)
			if err != nil {
				t.Fatal(err)
			}
			for _, requirement := range strings.Fields(expected) {
				if strings.Contains(string(contents), requirement) {
					t.Fatalf("generated pyproject exposes compiler-owned %q:\n%s", requirement, contents)
				}
			}
			assertContainsAll(t, "generated pyproject", string(contents), []string{
				`name = "` + framework + `-agent"`,
				`requires-python = ">=3.12,<3.13"`,
				`dependencies = []`,
				`[tool.uv]`,
				`package = false`,
			})
		})
	}
}

func TestUnknownFrameworkHasNoCompatibilityContract(t *testing.T) {
	if _, err := frameworkRequirements("unknown"); err == nil {
		t.Fatal("expected unsupported framework error")
	}
}
