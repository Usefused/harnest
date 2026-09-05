package main

import (
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
	runtimeRequirementsLockFile = "harnest-runtime.lock"
	maxDependencyFileBytes      = 16 * 1024 * 1024
)

// runtimeDependencyPlan is the filesystem-only input used before Python imports.
type runtimeDependencyPlan struct {
	ProjectFiles []string
	HasTasks     bool
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

// pluginRuntimeRequirements collects canonical extensions and legacy runtime packages.
func pluginRuntimeRequirements(root string) ([]string, []string, error) {
	var requirements, projects []string
	for _, folder := range []string{"plugins", "extensions"} {
		values, files, err := packageRuntimeRequirements(filepath.Join(root, folder), folder == "extensions")
		if err != nil {
			return nil, nil, err
		}
		requirements = append(requirements, values...)
		projects = append(projects, files...)
	}
	sort.Strings(projects)
	return requirements, projects, nil
}

// packageRuntimeRequirements inspects manifest-bearing packages, never legacy hook files.
func packageRuntimeRequirements(directory string, canonical bool) ([]string, []string, error) {
	entries, err := optionalRegularDirectoryEntries(directory)
	if err != nil {
		return nil, nil, err
	}
	var requirements, projects []string
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".") || strings.HasPrefix(entry.Name(), "_") {
			continue
		}
		if canonical && !entry.IsDir() {
			continue // Legacy lifecycle files are validated by the Python layout resolver.
		}
		values, project, found, projectErr := pluginRuntimeProject(directory, entry, canonical)
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
	directory string, entry os.DirEntry, canonical bool,
) ([]string, string, bool, error) {
	pluginDirectory := filepath.Join(directory, entry.Name())
	if !entry.IsDir() {
		return nil, "", false, fmt.Errorf("runtime plugin path must be a directory: %s\n\nWhat Harnest expects: one subfolder per plugin, not loose files in plugins/.\nHow to fix: put an active plugin in its own folder. If %q is only a note, backup, or unused example, rename it to %q or move it outside plugins/. Names starting with _ are left out of automatic discovery", pluginDirectory, entry.Name(), "_"+entry.Name())
	}
	manifest := filepath.Join(pluginDirectory, "plugin.yaml")
	if canonical {
		manifest = filepath.Join(pluginDirectory, "extension.yaml")
	}
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
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return false, nil
	} else if err != nil {
		return false, fmt.Errorf("inspect %s: %w", label, err)
	}
	if !info.Mode().IsRegular() {
		return false, fmt.Errorf("%s must be a regular file, not a symlink or directory: %s", label, path)
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

// replaceRegularFile atomically replaces generated dependency state without following links.
func replaceRegularFile(path string, contents []byte) error {
	return replaceRegularFileMode(path, contents, 0o600)
}

// replaceRegularFileMode applies final permissions before the atomic publication rename.
func replaceRegularFileMode(path string, contents []byte, mode os.FileMode) error {
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
	if err := temporary.Chmod(mode); err != nil {
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
