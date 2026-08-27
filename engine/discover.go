package engine

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

func Discover(plan DeploymentPlan) ([]Bundle, error) {
	if err := plan.Validate(); err != nil {
		return nil, err
	}
	projectRoot, err := filepath.Abs(plan.ProjectRoot)
	if err != nil {
		return nil, fmt.Errorf("resolve project root: %w", err)
	}
	projectRoot, err = filepath.EvalSymlinks(projectRoot)
	if err != nil {
		return nil, fmt.Errorf("resolve project root symlinks: %w", err)
	}
	projectInfo, err := os.Stat(projectRoot)
	if err != nil {
		return nil, fmt.Errorf("stat project root: %w", err)
	}
	if !projectInfo.IsDir() {
		return nil, fmt.Errorf("project root %s is not a directory", projectRoot)
	}
	var directories []string
	seenDirectories := map[string]struct{}{}
	for _, source := range plan.Sources {
		root := source.Root
		if !filepath.IsAbs(root) {
			root = filepath.Join(projectRoot, root)
		}
		root = filepath.Clean(root)
		if !within(projectRoot, root) {
			return nil, fmt.Errorf("agent source %q escapes project root", source.Root)
		}
		root, err = filepath.EvalSymlinks(root)
		if err != nil {
			return nil, fmt.Errorf("resolve agent source %s: %w", root, err)
		}
		if !within(projectRoot, root) {
			return nil, fmt.Errorf("agent source %q resolves outside project root", source.Root)
		}
		entries, err := os.ReadDir(root)
		if err != nil {
			return nil, fmt.Errorf("read agent source %s: %w", root, err)
		}
		include := source.Include
		if len(include) == 0 {
			include = []string{"*"}
		}
		for _, entry := range entries {
			if !entry.IsDir() || !matchesAny(entry.Name(), include) || matchesAny(entry.Name(), source.Exclude) {
				continue
			}
			directory := filepath.Join(root, entry.Name())
			if _, err := os.Stat(filepath.Join(directory, "config.yaml")); err != nil {
				if os.IsNotExist(err) {
					continue
				}
				return nil, fmt.Errorf("inspect agent config in %s: %w", directory, err)
			}
			if _, exists := seenDirectories[directory]; !exists {
				seenDirectories[directory] = struct{}{}
				directories = append(directories, directory)
			}
		}
	}
	sort.Strings(directories)

	bundles := make([]Bundle, 0, len(directories))
	seenNames := map[string]string{}
	for _, directory := range directories {
		bundle, err := LoadBundle(directory)
		if err != nil {
			return nil, err
		}
		if previous, exists := seenNames[bundle.Config.Metadata.Name]; exists {
			return nil, fmt.Errorf("duplicate agent name %q in %s and %s", bundle.Config.Metadata.Name, previous, directory)
		}
		seenNames[bundle.Config.Metadata.Name] = directory
		if bundle.Config.Spec.IsEnabled() {
			bundle.Labels = mergeLabels(plan.Labels, bundle.Config.Metadata.Labels)
			bundles = append(bundles, bundle)
		}
	}
	if len(bundles) == 0 {
		return nil, fmt.Errorf("deployment plan discovered no enabled agents")
	}
	return bundles, nil
}

func mergeLabels(planLabels, agentLabels map[string]string) map[string]string {
	labels := make(map[string]string, len(planLabels)+len(agentLabels))
	for key, value := range planLabels {
		labels[key] = value
	}
	for key, value := range agentLabels {
		labels[key] = value
	}
	return labels
}

func matchesAny(name string, patterns []string) bool {
	for _, pattern := range patterns {
		if matched, err := filepath.Match(pattern, name); err == nil && matched {
			return true
		}
	}
	return false
}
