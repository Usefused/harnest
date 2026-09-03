package main

import (
	"fmt"
	"strings"
)

// frameworkCompatibility is the compiled runtime dependency contract owned by
// this release. It includes the selected framework and built-in store drivers,
// while agent-selected model/provider packages remain project dependencies.
type frameworkCompatibility struct {
	RuntimeRequirements []string
}

var frameworkCompatibilityByName = map[string]frameworkCompatibility{
	"adk": {
		RuntimeRequirements: []string{
			"google-adk[eval,extensions,mcp]>=2.8,<3",
			"asyncpg>=0.30,<1",
			"redis>=6,<8",
		},
	},
	"langgraph": {
		RuntimeRequirements: []string{
			"google-adk[eval,extensions]>=2.8,<3",
			"langgraph>=1.2,<2",
			"langchain>=1.3,<2",
			"langchain-litellm>=0.7,<1",
			"langchain-mcp-adapters>=0.3,<1",
			"asyncpg>=0.30,<1",
			"redis>=6,<8",
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
	return strings.Join(compatibility.RuntimeRequirements, "\n") + "\n", nil
}
