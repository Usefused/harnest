package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFrameworkRequirementsAreReleaseOwned(t *testing.T) {
	tests := map[string]string{
		"adk": "google-adk[eval,extensions,mcp]>=2.8,<3\n" +
			"asyncpg>=0.30,<1\n" +
			"redis>=6,<8\n",
		"langgraph": "google-adk[eval]>=2.8,<3\n" +
			"langgraph>=1.2,<2\n" +
			"langchain>=1.3,<2\n" +
			"langchain-litellm>=0.7,<1\n" +
			"langchain-mcp-adapters>=0.3,<1\n" +
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
