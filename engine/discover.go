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
	projectRoot, err := resolveProjectRoot(plan.ProjectRoot)
	if err != nil {
		return nil, err
	}
	directories, err := discoverDirectories(projectRoot, plan.Sources)
	if err != nil {
		return nil, err
	}
	return loadDiscoveredBundles(directories, plan.Labels)
}

func resolveProjectRoot(projectRoot string) (string, error) {
	absolute, err := filepath.Abs(projectRoot)
	if err != nil {
		return "", fmt.Errorf("resolve project root: %w", err)
	}
	resolved, err := filepath.EvalSymlinks(absolute)
	if err != nil {
		return "", fmt.Errorf("resolve project root symlinks: %w", err)
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return "", fmt.Errorf("stat project root: %w", err)
	}
	if !info.IsDir() {
		return "", fmt.Errorf("project root %s is not a directory", resolved)
	}
	return resolved, nil
}

func discoverDirectories(projectRoot string, sources []AgentSource) ([]string, error) {
	var directories []string
	seenDirectories := map[string]struct{}{}
	for _, source := range sources {
		root, err := resolveSourceRoot(projectRoot, source.Root)
		if err != nil {
			return nil, err
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
			directory, selected, err := selectAgentDirectory(root, entry, include, source.Exclude)
			if err != nil {
				return nil, err
			}
			if selected {
				if _, exists := seenDirectories[directory]; !exists {
					seenDirectories[directory] = struct{}{}
					directories = append(directories, directory)
				}
			}
		}
	}
	sort.Strings(directories)
	return directories, nil
}

func resolveSourceRoot(projectRoot, sourceRoot string) (string, error) {
	root := sourceRoot
	if !filepath.IsAbs(root) {
		root = filepath.Join(projectRoot, root)
	}
	root = filepath.Clean(root)
	if !within(projectRoot, root) {
		return "", fmt.Errorf("agent source %q escapes project root", sourceRoot)
	}
	resolved, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", fmt.Errorf("resolve agent source %s: %w", root, err)
	}
	if !within(projectRoot, resolved) {
		return "", fmt.Errorf("agent source %q resolves outside project root", sourceRoot)
	}
	return resolved, nil
}

func selectAgentDirectory(root string, entry os.DirEntry, include, exclude []string) (string, bool, error) {
	if !entry.IsDir() || !matchesAny(entry.Name(), include) || matchesAny(entry.Name(), exclude) {
		return "", false, nil
	}
	directory := filepath.Join(root, entry.Name())
	_, err := os.Stat(filepath.Join(directory, "config.yaml"))
	if os.IsNotExist(err) {
		return "", false, nil
	}
	if err != nil {
		return "", false, fmt.Errorf("inspect agent config in %s: %w", directory, err)
	}
	return directory, true, nil
}

func loadDiscoveredBundles(directories []string, labels map[string]string) ([]Bundle, error) {
	bundles := make([]Bundle, 0, len(directories))
	seenNames := map[string]string{}
	for _, directory := range directories {
		// Disabled agents are still loaded so malformed bundles cannot hide from
		// deployment validation merely by toggling spec.enabled.
		bundle, err := LoadBundle(directory)
		if err != nil {
			return nil, err
		}
		if previous, exists := seenNames[bundle.Config.Metadata.Name]; exists {
			return nil, fmt.Errorf("duplicate agent name %q in %s and %s", bundle.Config.Metadata.Name, previous, directory)
		}
		seenNames[bundle.Config.Metadata.Name] = directory
		if bundle.Config.Spec.IsEnabled() {
			bundle.Labels = mergeLabels(labels, bundle.Config.Metadata.Labels)
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
