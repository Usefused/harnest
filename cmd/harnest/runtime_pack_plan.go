// Merges several agents' requirements into one shared runtime pack plan.

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
	"harnest.dev/harnest/internal/packbinary"
)

type runtimePackPlan struct {
	Owners                []engine.Bundle
	Python                string
	Requirements          []string
	CompileRequirements   []string
	Pins                  []string
	Projects              []string
	Inputs                []string
	PackDigest            string
	PackWheelsDir         string
	PackWheels            []packbinary.WheelEntry
	PackRequirements      []string
	PackExtensionProjects []string
}

// sharedRuntimePlan solves all agents together, rather than combining independently resolved environments.
func sharedRuntimePlan(agents []string) (runtimePackPlan, error) {
	return sharedRuntimePlanWithPack(agents, nil)
}

// sharedRuntimePlanWithPack resolves all agents together with any embedded pack dependencies.
func sharedRuntimePlanWithPack(agents []string, embedded *packbinary.LoadedPack) (runtimePackPlan, error) {
	var result runtimePackPlan
	if embedded != nil {
		result.PackDigest = embedded.Manifest.Digest
		if len(embedded.Manifest.Wheels) > 0 {
			result.PackWheelsDir = embedded.WheelsDir()
			result.PackWheels = append([]packbinary.WheelEntry{}, embedded.Manifest.Wheels...)
		}
		result.PackRequirements = append([]string{}, embedded.Manifest.Requirements...)
		result.PackExtensionProjects = embedded.ExtensionProjectFiles()
	}
	for _, directory := range agents {
		if err := appendAgentRuntimePlan(&result, directory, embedded); err != nil {
			return result, err
		}
	}
	result.Requirements = uniqueSorted(result.Requirements)
	result.Inputs = uniqueSorted(result.Inputs)
	return result, nil
}

// appendAgentRuntimePlan validates one agent and merges its dependency requirements into result.
func appendAgentRuntimePlan(result *runtimePackPlan, directory string, embedded *packbinary.LoadedPack) error {
	bundle, err := loadAgentBundle(directory)
	if err != nil {
		return err
	}
	if err = validateAgentDependencyPolicyWithPack(bundle, embedded); err != nil {
		return err
	}
	pin, err := lockedFrameworkRequirement(bundle)
	if err != nil {
		return err
	}
	if pin != "" {
		result.Pins = append(result.Pins, pin)
	}
	plan, err := inspectCompileDependencyPlan(bundle)
	if err != nil {
		return err
	}
	attachEmbeddedPackPlan(&plan, embedded)
	if result.Python != "" && result.Python != bundle.Config.Spec.Runtime.Version {
		return fmt.Errorf("shared agents must request the same Python version; build separate runtime packs")
	}
	result.Python = bundle.Config.Spec.Runtime.Version
	result.Projects = append(result.Projects, plan.ProjectFiles...)
	result.CompileRequirements = append(result.CompileRequirements, plan.CompileRequirements...)
	result.Owners = append(result.Owners, bundle)
	extras := runtimeEnvironmentProfile.wheelExtras(bundle.Config.Spec.Framework.Name, plan)
	result.Requirements = append(result.Requirements, extras...)
	identity, err := runtimePackInput(bundle, plan)
	if err != nil {
		return err
	}
	result.Inputs = append(result.Inputs, identity)
	return nil
}

// runtimePackInput binds dependency declarations, not source paths or unrelated agent code.
func runtimePackInput(bundle engine.Bundle, plan runtimeDependencyPlan) (string, error) {
	requirements := append([]string{}, plan.CompileRequirements...)
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
		Python, Framework, Lock, Pack string
		MCP                           bool
		Requirements                  []string
	}{bundle.Config.Spec.Runtime.Version, bundle.Config.Spec.Framework.Name, lock, plan.PackDigest, plan.HasMCP, uniqueSorted(requirements)}
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

// writePackRequirements includes the release wheel, embedded pack packages, and leaves provider resolution to uv.
func writePackRequirements(root, wheel string, plan runtimePackPlan) (string, error) {
	filename := filepath.Join(root, "runtime.in")
	requirement := fmt.Sprintf("harnest[%s] @ %s\n", strings.Join(plan.Requirements, ","), runtimeWheelURI(wheel))
	requirements := append(append([]string{}, plan.Pins...), plan.CompileRequirements...)
	for _, entry := range plan.PackWheels {
		wheelPath := filepath.Join(plan.PackWheelsDir, filepath.Base(entry.Filename))
		requirements = append(requirements, fmt.Sprintf("%s @ %s", entry.Name, runtimeWheelURI(wheelPath)))
	}
	requirements = append(requirements, plan.PackRequirements...)
	return filename, os.WriteFile(filename, []byte(requirement+strings.Join(uniqueSorted(requirements), "\n")+"\n"), 0600)
}
