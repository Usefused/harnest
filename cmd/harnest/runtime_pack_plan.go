package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"harnest.dev/harnest/engine"
)

type runtimePackPlan struct {
	Owners       []engine.Bundle
	Python       string
	Requirements []string
	Pins         []string
	Projects     []string
	Inputs       []string
}

// sharedRuntimePlan solves all agents together, rather than combining independently resolved environments.
func sharedRuntimePlan(agents []string) (runtimePackPlan, error) {
	var result runtimePackPlan
	for _, directory := range agents {
		bundle, err := loadAgentBundle(directory)
		if err != nil {
			return result, err
		}
		if err = validateAgentDependencyPolicy(bundle); err != nil {
			return result, err
		}
		pin, err := lockedFrameworkRequirement(bundle)
		if err != nil {
			return result, err
		}
		if pin != "" {
			result.Pins = append(result.Pins, pin)
		}
		plan, err := inspectRuntimeDependencyPlan(bundle)
		if err != nil {
			return result, err
		}
		if result.Python != "" && result.Python != bundle.Config.Spec.Runtime.Version {
			return result, fmt.Errorf("shared agents must request the same Python version; build separate runtime packs")
		}
		result.Python = bundle.Config.Spec.Runtime.Version
		result.Projects = append(result.Projects, plan.ProjectFiles...)
		result.Owners = append(result.Owners, bundle)
		extras := runtimeEnvironmentProfile.wheelExtras(bundle.Config.Spec.Framework.Name, plan)
		result.Requirements = append(result.Requirements, extras...)
		identity, err := runtimePackInput(bundle, plan)
		if err != nil {
			return result, err
		}
		result.Inputs = append(result.Inputs, identity)
	}
	result.Requirements = uniqueSorted(result.Requirements)
	result.Inputs = uniqueSorted(result.Inputs)
	return result, nil
}

// runtimePackInput binds dependency declarations, not source paths or unrelated agent code.
func runtimePackInput(bundle engine.Bundle, plan runtimeDependencyPlan) (string, error) {
	var requirements []string
	for _, project := range plan.ProjectFiles {
		values, err := projectRuntimeRequirements(project, "runtime pack")
		if err != nil {
			return "", err
		}
		requirements = append(requirements, values...)
	}
	lock, err := packLockIdentity(bundle)
	if err != nil {
		return "", err
	}
	value := struct {
		Python, Framework, Lock string
		MCP                     bool
		Requirements            []string
	}{bundle.Config.Spec.Runtime.Version, bundle.Config.Spec.Framework.Name, lock, plan.HasMCP, uniqueSorted(requirements)}
	data, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]), nil
}

// uniqueSorted prevents duplicate owners from inflating dependency input identities.
func uniqueSorted(values []string) []string {
	seen := make(map[string]bool)
	var result []string
	for _, value := range values {
		if !seen[value] {
			seen[value] = true
			result = append(result, value)
		}
	}
	sort.Strings(result)
	return result
}

// writePackRequirements includes the release wheel and leaves provider resolution to uv.
func writePackRequirements(root, wheel string, plan runtimePackPlan) (string, error) {
	filename := filepath.Join(root, "runtime.in")
	requirement := fmt.Sprintf("harnest[%s] @ %s\n", strings.Join(plan.Requirements, ","), runtimeWheelURI(wheel))
	return filename, os.WriteFile(filename, []byte(requirement+strings.Join(uniqueSorted(plan.Pins), "\n")+"\n"), 0600)
}
