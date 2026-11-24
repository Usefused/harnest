// Rejects agent requirements that pin packages the Harnest compiler owns, such as Harnest itself and framework SDKs.

package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/pelletier/go-toml/v2"

	"harnest.dev/harnest/engine"
	"harnest.dev/harnest/internal/frameworks"
	"harnest.dev/harnest/internal/packbinary"
)

var (
	distributionNamePattern   = regexp.MustCompile(`^[[:space:]]*([A-Za-z0-9][A-Za-z0-9._-]*)`)
	distributionSeparatorRuns = regexp.MustCompile(`[-_.]+`)
)

// validateAgentDependencyPolicy enforces Harnest's compiler-owned dependency boundaries.
func validateAgentDependencyPolicy(bundle engine.Bundle) error {
	return validateAgentDependencyPolicyWithPack(bundle, nil)
}

// validateAgentDependencyPolicyWithPack rejects agent overrides of compiler- or pack-owned packages.
func validateAgentDependencyPolicyWithPack(bundle engine.Bundle, embedded *packbinary.LoadedPack) error {
	path := filepath.Join(bundle.Directory, bundle.Config.Spec.Runtime.DependencyFile)
	contents, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read agent pyproject.toml: %w", err)
	}
	var document map[string]any
	if err := toml.Unmarshal(contents, &document); err != nil {
		return fmt.Errorf("parse agent pyproject.toml: %w", err)
	}
	owned, err := compilerOwnedDistributions(bundle.Config.Spec.Framework.Name)
	if err != nil {
		return err
	}
	protected := packProtectedDistributions(embedded)
	rootRequirements := authoredDependencyRequirements(document)
	if err := rejectOwnedRequirements(rootRequirements, "pyproject.toml", owned, protected, embedded); err != nil {
		return err
	}
	plan, err := inspectRuntimeDependencyPlan(bundle)
	if err != nil {
		return err
	}
	for _, project := range plan.ProjectFiles[1:] {
		requirements, projectErr := projectRuntimeRequirements(project, "Harnest Extension")
		if projectErr != nil {
			return projectErr
		}
		if err := rejectOwnedRequirements(requirements, project, owned, protected, embedded); err != nil {
			return err
		}
	}
	return nil
}

// rejectOwnedRequirements checks both compiler-owned and pack-owned distribution policies.
func rejectOwnedRequirements(
	requirements []string,
	source string,
	owned map[string]struct{},
	protected map[string]struct{},
	embedded *packbinary.LoadedPack,
) error {
	if err := rejectCompilerOwnedRequirements(requirements, source, owned); err != nil {
		return err
	}
	if len(protected) == 0 || embedded == nil {
		return nil
	}
	for _, requirement := range requirements {
		name := normalizedRequirementName(requirement)
		if _, exists := protected[name]; exists {
			return fmt.Errorf(
				"%s must not declare pack-owned package %q; upgrade %s to change pack-managed packages",
				source, name, embedded.Manifest.Command,
			)
		}
	}
	return nil
}

// packProtectedDistributions collects normalized distribution names enforced by an embedded pack.
func packProtectedDistributions(embedded *packbinary.LoadedPack) map[string]struct{} {
	if embedded == nil || len(embedded.Manifest.ProtectedPackages) == 0 {
		return nil
	}
	protected := make(map[string]struct{}, len(embedded.Manifest.ProtectedPackages))
	for _, name := range embedded.Manifest.ProtectedPackages {
		if normalized := normalizedRequirementName(name); normalized != "" {
			protected[normalized] = struct{}{}
		}
	}
	return protected
}

// rejectCompilerOwnedRequirements applies one package-ownership policy per project.
func rejectCompilerOwnedRequirements(
	requirements []string, source string, owned map[string]struct{},
) error {
	for _, requirement := range requirements {
		if err := rejectCompilerOwnedRequirement(requirement, source, owned); err != nil {
			return err
		}
	}
	return nil
}

// rejectCompilerOwnedRequirement keeps one diagnostic shared by all projects.
func rejectCompilerOwnedRequirement(requirement, source string, owned map[string]struct{}) error {
	name := normalizedRequirementName(requirement)
	if _, exists := owned[name]; !exists {
		return nil
	}
	return fmt.Errorf(
		"%s must not declare compiler-owned framework package %q; upgrade Harnest to change framework versions",
		source, name,
	)
}

func compilerOwnedDistributions(selectedFramework string) (map[string]struct{}, error) {
	if _, err := compatibilityForFramework(selectedFramework); err != nil {
		return nil, err
	}
	return allCompilerOwnedDistributions(), nil
}

// allCompilerOwnedDistributions identifies packages supplied by Harnest runtime profiles.
func allCompilerOwnedDistributions() map[string]struct{} {
	owned := map[string]struct{}{"harnest": {}}
	for _, framework := range frameworks.All() {
		requirements := append(framework.RuntimeRequirements(), framework.MCPRequirements...)
		for _, requirement := range requirements {
			owned[normalizedRequirementName(requirement)] = struct{}{}
		}
	}
	return owned
}

func authoredDependencyRequirements(document map[string]any) []string {
	requirements := []string{}
	project := tomlTable(document["project"])
	appendDependencyStrings(&requirements, project["dependencies"])
	appendTableDependencyStrings(&requirements, project["optional-dependencies"])
	appendTableDependencyStrings(&requirements, document["dependency-groups"])
	tool := tomlTable(document["tool"])
	uv := tomlTable(tool["uv"])
	for _, key := range []string{
		"override-dependencies",
		"constraint-dependencies",
		"build-constraint-dependencies",
	} {
		appendDependencyStrings(&requirements, uv[key])
	}
	return requirements
}

func appendTableDependencyStrings(target *[]string, value any) {
	for _, nested := range tomlTable(value) {
		appendDependencyStrings(target, nested)
	}
}

func appendDependencyStrings(target *[]string, value any) {
	switch typed := value.(type) {
	case string:
		*target = append(*target, typed)
	case []any:
		for _, nested := range typed {
			appendDependencyStrings(target, nested)
		}
	}
}

func tomlTable(value any) map[string]any {
	table, _ := value.(map[string]any)
	return table
}

func normalizedRequirementName(requirement string) string {
	match := distributionNamePattern.FindStringSubmatch(requirement)
	if len(match) != 2 {
		return ""
	}
	return distributionSeparatorRuns.ReplaceAllString(strings.ToLower(match[1]), "-")
}
