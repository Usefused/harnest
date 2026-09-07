package main

import (
	"fmt"
	"strings"
)

type environmentProfile string

const (
	runtimeEnvironmentProfile environmentProfile = "runtime"
	testEnvironmentProfile    environmentProfile = "test"
	evalEnvironmentProfile    environmentProfile = "eval"
)

var environmentProfiles = []environmentProfile{
	runtimeEnvironmentProfile,
	testEnvironmentProfile,
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
	return "", fmt.Errorf("--profile must be runtime, test, or eval")
}

// stateFile keeps the existing runtime pointer compatible while isolating tools.
func (p environmentProfile) stateFile() string {
	if p == runtimeEnvironmentProfile {
		return environmentStateFile
	}
	return fmt.Sprintf("environment-%s.json", p)
}

// requirementsLockFile keeps deploy/runtime resolution independent from dev tools.
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
	if p == testEnvironmentProfile {
		extras = append(extras, "test")
	}
	if p == evalEnvironmentProfile {
		extras = append(extras, "eval")
	}
	return extras
}
