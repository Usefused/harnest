package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/pelletier/go-toml/v2"

	"harnest.dev/harnest/engine"
)

var (
	distributionNamePattern   = regexp.MustCompile(`^[[:space:]]*([A-Za-z0-9][A-Za-z0-9._-]*)`)
	distributionSeparatorRuns = regexp.MustCompile(`[-_.]+`)
)

func validateAgentDependencyPolicy(bundle engine.Bundle) error {
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
	for _, requirement := range authoredDependencyRequirements(document) {
		name := normalizedRequirementName(requirement)
		if _, exists := owned[name]; exists {
			return fmt.Errorf(
				"pyproject.toml must not declare compiler-owned framework package %q; upgrade Harnest to change framework versions",
				name,
			)
		}
	}
	return nil
}

func compilerOwnedDistributions(selectedFramework string) (map[string]struct{}, error) {
	if _, err := compatibilityForFramework(selectedFramework); err != nil {
		return nil, err
	}
	owned := map[string]struct{}{"harnest": {}}
	for _, compatibility := range frameworkCompatibilityByName {
		for _, requirement := range compatibility.FrameworkRequirements {
			owned[normalizedRequirementName(requirement)] = struct{}{}
		}
	}
	return owned, nil
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
