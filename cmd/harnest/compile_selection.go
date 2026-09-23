package main

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/pelletier/go-toml/v2"
	"gopkg.in/yaml.v3"
	"harnest.dev/harnest/engine"
)

const compileSelectionFile = "harnest-compile.yaml"

var compileExtraName = regexp.MustCompile(`^[A-Za-z0-9]+([._-][A-Za-z0-9]+)*$`)

type compileSelection struct {
	Extras []string `json:"extras"`
}

// readCompileSelection accepts inert, versioned declarations without loading project packs.
func readCompileSelection(root string) (compileSelection, error) {
	data, err := readRegularDependencyFile(filepath.Join(root, compileSelectionFile))
	if os.IsNotExist(err) {
		return compileSelection{}, nil
	}
	if err != nil {
		return compileSelection{}, err
	}
	decoder := yaml.NewDecoder(bytes.NewReader(data))
	var document map[string]any
	if err = decoder.Decode(&document); err != nil {
		return compileSelection{}, err
	}
	var trailing any
	if err = decoder.Decode(&trailing); err != io.EOF {
		return compileSelection{}, fmt.Errorf("%s must contain one YAML document", compileSelectionFile)
	}
	return parseCompileSelection(document)
}

// parseCompileSelection rejects misspellings rather than silently omitting team requirements.
func parseCompileSelection(document map[string]any) (compileSelection, error) {
	var result compileSelection
	if version, ok := document["version"].(int); !ok || version != 1 {
		return result, fmt.Errorf("%s requires version: 1", compileSelectionFile)
	}
	for key := range document {
		if key != "version" && key != "extras" {
			return result, fmt.Errorf("unknown %s field %q", compileSelectionFile, key)
		}
	}
	var err error
	result.Extras, err = compileSelectionStrings(document, "extras")
	if err != nil {
		return result, err
	}
	return result, validateCompileSelection(result)
}

// compileSelectionStrings distinguishes an omitted list from malformed explicit nulls.
func compileSelectionStrings(document map[string]any, key string) ([]string, error) {
	value, exists := document[key]
	return strictStringList(value, compileSelectionFile+"."+key, !exists)
}

// validateCompileSelection restricts selections to Python optional-dependency group names.
func validateCompileSelection(value compileSelection) error {
	for _, extra := range value.Extras {
		if !compileExtraName.MatchString(extra) {
			return fmt.Errorf("invalid compile extra %q", extra)
		}
	}
	return nil
}

// compileExtraRequirements selects only named root-project extras; base and extension requirements remain mandatory.
func compileExtraRequirements(project string, extras []string) ([]string, error) {
	if len(extras) == 0 {
		return nil, nil
	}
	data, err := readRegularDependencyFile(project)
	if err != nil {
		return nil, err
	}
	var document map[string]any
	if err = toml.Unmarshal(data, &document); err != nil {
		return nil, err
	}
	groups := tomlTable(tomlTable(document["project"])["optional-dependencies"])
	var requirements []string
	for _, extra := range uniqueSorted(extras) {
		values, err := strictStringList(groups[extra], "[project.optional-dependencies]."+extra, false)
		if err != nil {
			return nil, err
		}
		if err := validateCompileRequirements(values); err != nil {
			return nil, err
		}
		requirements = append(requirements, values...)
	}
	return uniqueSorted(requirements), nil
}

// inspectCompileDependencyPlan keeps compile-only selections outside ordinary agent environments.
func inspectCompileDependencyPlan(bundle engine.Bundle) (runtimeDependencyPlan, error) {
	plan, err := inspectRuntimeDependencyPlan(bundle)
	if err != nil {
		return plan, err
	}
	selection, err := readCompileSelection(bundle.Directory)
	if err != nil {
		return plan, err
	}
	plan.CompileRequirements, err = compileExtraRequirements(plan.ProjectFiles[0], selection.Extras)
	return plan, err
}

// inspectProfileDependencyPlan isolates optional compilation dependencies from regular runs.
func inspectProfileDependencyPlan(bundle engine.Bundle, profile environmentProfile) (runtimeDependencyPlan, error) {
	if profile == compileEnvironmentProfile {
		return inspectCompileDependencyPlan(bundle)
	}
	return inspectRuntimeDependencyPlan(bundle)
}

// compileProfile preserves the existing production environment when no optional packages are requested.
func compileProfile(bundle engine.Bundle) (environmentProfile, error) {
	plan, err := inspectCompileDependencyPlan(bundle)
	if err != nil {
		return "", err
	}
	if len(plan.CompileRequirements) > 0 {
		return compileEnvironmentProfile, nil
	}
	return runtimeEnvironmentProfile, nil
}

// validateCompileRequirements prevents PEP 621 entries from becoming requirements-file directives.
func validateCompileRequirements(values []string) error {
	for _, value := range values {
		if strings.ContainsAny(value, "\r\n") || !distributionNamePattern.MatchString(value) {
			return fmt.Errorf("compile extra must contain Python package requirements, got %q", value)
		}
	}
	return nil
}
