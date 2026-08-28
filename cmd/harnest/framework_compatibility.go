package main

import (
	"fmt"
	"strings"
)

// frameworkCompatibility is the framework dependency contract owned by this
// Harnest release. Keep provider-specific dependencies out of this list: these
// requirements are only the packages Harnest itself integrates with.
type frameworkCompatibility struct {
	FrameworkRequirements []string
}

var frameworkCompatibilityByName = map[string]frameworkCompatibility{
	"adk": {
		FrameworkRequirements: []string{
			"google-adk[eval,extensions,mcp]>=2.8,<3",
		},
	},
	"langgraph": {
		FrameworkRequirements: []string{
			"langgraph>=1.2,<2",
			"langchain>=1.3,<2",
			"langchain-litellm>=0.7,<1",
			"langchain-mcp-adapters>=0.3,<1",
		},
	},
}

func compatibilityForFramework(framework string) (frameworkCompatibility, error) {
	compatibility, ok := frameworkCompatibilityByName[framework]
	if !ok {
		return frameworkCompatibility{}, fmt.Errorf("unsupported framework %q", framework)
	}
	return compatibility, nil
}

func frameworkRequirements(framework string) (string, error) {
	compatibility, err := compatibilityForFramework(framework)
	if err != nil {
		return "", err
	}
	return strings.Join(compatibility.FrameworkRequirements, "\n") + "\n", nil
}
