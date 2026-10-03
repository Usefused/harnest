package main

import (
	"fmt"
	"strings"
)

type environmentProfile string

const (
	runtimeEnvironmentProfile     environmentProfile = "runtime"
	compileEnvironmentProfile     environmentProfile = "compile"
	developmentEnvironmentProfile environmentProfile = "development"
	evalEnvironmentProfile        environmentProfile = "eval"
)

var environmentProfiles = []environmentProfile{
	compileEnvironmentProfile,
	runtimeEnvironmentProfile,
	developmentEnvironmentProfile,
	evalEnvironmentProfile,
}

// parseEnvironmentProfile converts the public flag into a closed profile set.
func parseEnvironmentProfile(value string) (environmentProfile, error) {
	profile := environmentProfile(strings.TrimSpace(value))
	for _, candidate := range environmentProfiles {
		if profile == candidate {
			return profile, nil
		}
	}
	return "", fmt.Errorf("--profile must be runtime, compile, development, or eval")
}

// directoryName maps dependency profiles onto the two reusable project environments.
func (p environmentProfile) directoryName() string {
	if p == developmentEnvironmentProfile || p == evalEnvironmentProfile {
		return "eval"
	}
	return "agent"
}

// stateFile shares a publication pointer so switching profiles invalidates the old cache.
func (p environmentProfile) stateFile() string {
	if p.directoryName() == "agent" {
		return environmentStateFile
	}
	return "environment-eval.json"
}

// requirementsLockFile keeps production resolution independent from development tools.
func (p environmentProfile) requirementsLockFile() string {
	if p == runtimeEnvironmentProfile {
		return runtimeRequirementsLockFile
	}
	return fmt.Sprintf("harnest-%s.lock", p)
}

// wheelExtras selects only capabilities required by this command and source tree.
func (p environmentProfile) wheelExtras(
	framework string, plan runtimeDependencyPlan,
) []string {
	extras := []string{framework}
	if plan.HasMCP {
		extras = append(extras, framework+"-mcp")
	}
	if p == developmentEnvironmentProfile {
		extras = append(extras, "test")
	}
	if p == evalEnvironmentProfile {
		extras = append(extras, "eval")
	}
	return extras
}
