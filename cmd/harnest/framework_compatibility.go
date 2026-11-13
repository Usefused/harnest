// Framework package requirements, derived from the shared metadata in internal/frameworks.

package main

import (
	"fmt"
	"strings"

	"harnest.dev/harnest/internal/frameworks"
)

// frameworkCompatibility is the compiled runtime dependency contract owned by
// this release. It includes the selected framework and built-in store drivers,
// while agent-selected model/provider packages remain project dependencies.
// Values come from the shared frameworks.json metadata.
type frameworkCompatibility struct {
	RuntimeRequirements []string
	MCPRequirements     []string
}

// compatibilityForFramework returns the release-owned requirements for one framework.
func compatibilityForFramework(framework string) (frameworkCompatibility, error) {
	selected, ok := frameworks.Lookup(framework)
	if !ok {
		return frameworkCompatibility{}, fmt.Errorf("unsupported framework %q", framework)
	}
	return frameworkCompatibility{
		RuntimeRequirements: selected.RuntimeRequirements(),
		MCPRequirements:     append([]string{}, selected.MCPRequirements...),
	}, nil
}

func frameworkRequirements(framework string) (string, error) {
	compatibility, err := compatibilityForFramework(framework)
	if err != nil {
		return "", err
	}
	return strings.Join(compatibility.RuntimeRequirements, "\n") + "\n", nil
}
