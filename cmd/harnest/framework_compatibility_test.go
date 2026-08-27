package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestFrameworkRequirementsAreReleaseOwned(t *testing.T) {
	tests := map[string]string{
		"adk": "google-adk[eval,mcp]>=2.8,<3\n",
		"langgraph": "langgraph>=1.2,<2\n" +
			"langchain>=1.3,<2\n" +
			"langchain-litellm>=0.7,<1\n" +
			"langchain-mcp-adapters>=0.3,<1\n",
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

func TestScaffoldUsesReleaseFrameworkRequirements(t *testing.T) {
	for _, framework := range []string{"adk", "langgraph"} {
		framework := framework
		t.Run(framework, func(t *testing.T) {
			directory := filepath.Join(t.TempDir(), framework+"-agent")
			if err := createScaffoldForFramework(directory, framework+"-agent", framework); err != nil {
				t.Fatal(err)
			}
			contents, err := os.ReadFile(filepath.Join(directory, "requirements.txt"))
			if err != nil {
				t.Fatal(err)
			}
			expected, err := frameworkRequirements(framework)
			if err != nil {
				t.Fatal(err)
			}
			if string(contents) != expected {
				t.Fatalf("generated requirements = %q, want %q", contents, expected)
			}
		})
	}
}

func TestUnknownFrameworkHasNoCompatibilityContract(t *testing.T) {
	if _, err := frameworkRequirements("unknown"); err == nil {
		t.Fatal("expected unsupported framework error")
	}
}
