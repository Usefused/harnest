package main

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/pelletier/go-toml/v2"

	"harnest.dev/harnest/engine"
)

const procrastinateRequirement = "procrastinate==3.9.0"

const (
	runtimeRequirementsLockFile = "runtime-requirements.lock"
	runtimeTaskInputFile        = "runtime-tasks.in"
	maxDependencyFileBytes      = 16 * 1024 * 1024
)

// runtimeDependencyPlan is the filesystem-only input used before Python imports.
type runtimeDependencyPlan struct {
	ProjectFiles []string
	HasTasks     bool
}

// needsJointResolution reports whether dependencies exist outside the root project.
func (p runtimeDependencyPlan) needsJointResolution() bool {
	return len(p.ProjectFiles) > 1 || p.HasTasks
}

// inspectRuntimeDependencyPlan joins agent, plugin, and optional task requirements.
func inspectRuntimeDependencyPlan(bundle engine.Bundle) (runtimeDependencyPlan, error) {
	rootProject := filepath.Join(bundle.Directory, bundle.Config.Spec.Runtime.DependencyFile)
	_, err := projectRuntimeRequirements(rootProject, "agent")
	if err != nil {
		return runtimeDependencyPlan{}, err
	}
	_, projectFiles, err := pluginRuntimeRequirements(bundle.Directory)
	if err != nil {
		return runtimeDependencyPlan{}, err
	}
	hasTasks, err := hasAuthoredTasks(bundle.Directory)
	if err != nil {
		return runtimeDependencyPlan{}, err
	}
	return runtimeDependencyPlan{
		ProjectFiles: append([]string{rootProject}, projectFiles...),
		HasTasks:     hasTasks,
	}, nil
}

// projectRuntimeRequirements reads only static PEP 621 runtime dependencies.
func projectRuntimeRequirements(path, owner string) ([]string, error) {
	contents, err := readRegularDependencyFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s pyproject.toml: %w", owner, err)
	}
	var document map[string]any
	if err := toml.Unmarshal(contents, &document); err != nil {
		return nil, fmt.Errorf("parse %s pyproject.toml: %w", owner, err)
	}
	project := tomlTable(document["project"])
	if len(project) == 0 {
		return nil, fmt.Errorf("%s pyproject.toml must define a PEP 621 [project] table", owner)
	}
	dynamic, err := strictStringList(project["dynamic"], "[project].dynamic", true)
	if err != nil {
		return nil, fmt.Errorf("%s pyproject.toml: %w", owner, err)
	}
	if containsString(dynamic, "dependencies") {
		return nil, fmt.Errorf("%s dependencies must be static PEP 621 metadata", owner)
	}
	dependencies, err := strictStringList(project["dependencies"], "[project].dependencies", true)
	if err != nil {
		return nil, fmt.Errorf("%s pyproject.toml: %w", owner, err)
	}
	return dependencies, nil
}

// pluginRuntimeRequirements discovers only folders carrying plugin.yaml.
func pluginRuntimeRequirements(root string) ([]string, []string, error) {
	directory := filepath.Join(root, "plugins")
	entries, err := optionalRegularDirectoryEntries(directory)
	if err != nil {
		return nil, nil, err
	}
	var requirements, projects []string
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".") || strings.HasPrefix(entry.Name(), "_") {
			continue
		}
		values, project, found, projectErr := pluginRuntimeProject(directory, entry)
		if projectErr != nil {
			return nil, nil, projectErr
		}
		if !found {
			continue
		}
		requirements = append(requirements, values...)
		projects = append(projects, project)
	}
	sort.Strings(projects)
	return requirements, projects, nil
}

// pluginRuntimeProject resolves one manifest-owned optional dependency file.
func pluginRuntimeProject(
	directory string, entry os.DirEntry,
) ([]string, string, bool, error) {
	pluginDirectory := filepath.Join(directory, entry.Name())
	if !entry.IsDir() {
		return nil, "", false, fmt.Errorf("runtime plugin path must be a directory: %s\n\nWhat Harnest expects: one subfolder per plugin, not loose files in plugins/.\nHow to fix: put an active plugin in its own folder. If %q is only a note, backup, or unused example, rename it to %q or move it outside plugins/. Names starting with _ are left out of automatic discovery", pluginDirectory, entry.Name(), "_"+entry.Name())
	}
	manifest := filepath.Join(pluginDirectory, "plugin.yaml")
	found, err := regularDependencyPathExists(manifest, "runtime plugin manifest")
	if err != nil || !found {
		return nil, "", false, err
	}
	project := filepath.Join(pluginDirectory, "pyproject.toml")
	found, err = regularDependencyPathExists(project, "runtime plugin pyproject.toml")
	if err != nil || !found {
		return nil, "", false, err
	}
	values, err := projectRuntimeRequirements(project, "runtime plugin "+entry.Name())
	return values, project, true, err
}

// regularDependencyPathExists distinguishes an absent optional file from I/O failure.
func regularDependencyPathExists(path, label string) (bool, error) {
	if _, err := os.Lstat(path); os.IsNotExist(err) {
		return false, nil
	} else if err != nil {
		return false, fmt.Errorf("inspect %s: %w", label, err)
	}
	return true, nil
}

// hasAuthoredTasks mirrors Python's public tasks/*.py discovery without imports.
func hasAuthoredTasks(root string) (bool, error) {
	directory := filepath.Join(root, "tasks")
	entries, err := optionalRegularDirectoryEntries(directory)
	if err != nil {
		return false, err
	}
	for _, entry := range entries {
		name := entry.Name()
		if strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_") {
			continue
		}
		if entry.Type()&os.ModeSymlink != 0 || !entry.Type().IsRegular() || filepath.Ext(name) != ".py" {
			return false, fmt.Errorf("unexpected resource in tasks directory: %s\n\nWhat Harnest expects: real Python (.py) files directly inside tasks/, with one declared task per file; links and subfolders are not supported.\nHow to fix: put each task in its own .py file. Move notes, backups, and unused examples outside tasks/ or give them an _ prefix, such as _notes.md. A link must be replaced by a real file, not just renamed", filepath.Join(directory, name))
		}
		return true, nil
	}
	return false, nil
}

// optionalRegularDirectoryEntries rejects symlink roots before bounded discovery.
func optionalRegularDirectoryEntries(path string) ([]os.DirEntry, error) {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("inspect optional runtime directory %s: %w", path, err)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return nil, fmt.Errorf("runtime dependency path must be a regular directory: %s", path)
	}
	return os.ReadDir(path)
}

// readRegularDependencyFile prevents dependency metadata from escaping the bundle.
func readRegularDependencyFile(path string) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return nil, fmt.Errorf("dependency file must be regular: %s", path)
	}
	if info.Size() > maxDependencyFileBytes {
		return nil, fmt.Errorf(
			"dependency file exceeds %d bytes: %s", maxDependencyFileBytes, path,
		)
	}
	return os.ReadFile(path)
}

// strictStringList rejects TOML shapes that could otherwise hide dependencies.
func strictStringList(value any, label string, optional bool) ([]string, error) {
	if value == nil && optional {
		return nil, nil
	}
	items, ok := value.([]any)
	if !ok {
		return nil, fmt.Errorf("%s must be a list of strings", label)
	}
	result := make([]string, 0, len(items))
	for _, item := range items {
		text, ok := item.(string)
		if !ok || strings.TrimSpace(text) == "" {
			return nil, fmt.Errorf("%s must contain only non-empty strings", label)
		}
		result = append(result, text)
	}
	return result, nil
}

func containsString(values []string, expected string) bool {
	for _, value := range values {
		if value == expected {
			return true
		}
	}
	return false
}

// jointResolutionInputs creates the one generated input needed for @task.
func jointResolutionInputs(bundle engine.Bundle, plan runtimeDependencyPlan) ([]string, error) {
	inputs := append([]string{}, plan.ProjectFiles...)
	if !plan.HasTasks {
		return inputs, nil
	}
	path := filepath.Join(bundle.Directory, ".harnest", runtimeTaskInputFile)
	if err := replaceRegularFile(path, []byte(procrastinateRequirement+"\n")); err != nil {
		return nil, fmt.Errorf("write task runtime dependency input: %w", err)
	}
	return append(inputs, path), nil
}

// replaceRegularFile atomically replaces generated dependency state without following links.
func replaceRegularFile(path string, contents []byte) error {
	directory := filepath.Dir(path)
	temporary, err := os.CreateTemp(directory, ".harnest-dependencies-*")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	removeTemporary := true
	defer func() {
		_ = temporary.Close()
		if removeTemporary {
			_ = os.Remove(temporaryPath)
		}
	}()
	if _, err := temporary.Write(contents); err != nil {
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return err
	}
	removeTemporary = false
	return nil
}

// requireFrozenRuntimeLock ensures a joint lock exists before uv is invoked.
func requireFrozenRuntimeLock(path string) error {
	if _, err := readRegularDependencyFile(path); err != nil {
		return fmt.Errorf("frozen runtime dependency lock is unavailable: %w", err)
	}
	return nil
}

// publishRuntimeLock either verifies frozen output or atomically publishes it.
func publishRuntimeLock(candidate, destination string, frozen bool) error {
	candidateContents, err := readRegularDependencyFile(candidate)
	if err != nil {
		return fmt.Errorf("read resolved runtime dependency lock: %w", err)
	}
	if !frozen {
		return replaceRegularFile(destination, candidateContents)
	}
	lockedContents, err := readRegularDependencyFile(destination)
	if err != nil {
		return fmt.Errorf("read frozen runtime dependency lock: %w", err)
	}
	if !bytes.Equal(candidateContents, lockedContents) {
		return fmt.Errorf("runtime dependency lock is stale; run harnest env sync without --frozen")
	}
	return nil
}
