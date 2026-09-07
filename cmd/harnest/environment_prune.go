package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sync"
)

const environmentLeaseDirectory = "environment-leases"

var environmentFingerprintPattern = regexp.MustCompile(`^[a-f0-9]{16}$`)

// leaseAgentPython protects a managed environment for the caller's lifetime.
func leaseAgentPython(
	project string, selection pythonSelection,
) (pythonSelection, error) {
	name, managed := managedAgentEnvironment(project, selection.Executable)
	if !managed {
		return selection, nil
	}
	leaseRoot := filepath.Join(project, ".harnest", environmentLeaseDirectory)
	leaseDirectory := filepath.Join(leaseRoot, name)
	if err := ensureRegularLeaseDirectory(leaseDirectory); err != nil {
		return pythonSelection{}, err
	}
	lease, err := os.CreateTemp(leaseDirectory, "use-")
	if err != nil {
		return pythonSelection{}, fmt.Errorf("create agent environment lease: %w", err)
	}
	leasePath := lease.Name()
	if err := lease.Close(); err != nil {
		_ = os.Remove(leasePath)
		return pythonSelection{}, fmt.Errorf("close agent environment lease: %w", err)
	}
	var once sync.Once
	selection.release = func() {
		once.Do(func() {
			_ = os.Remove(leasePath)
			_ = os.Remove(leaseDirectory)
			pruneAgentEnvironmentsAsync(project)
		})
	}
	// A lease is visible before pruning starts, so overlapping commands cannot
	// lose dependencies after selecting an older but still valid interpreter.
	pruneAgentEnvironmentsAsync(project)
	return selection, nil
}

// managedAgentEnvironment recognizes only the compiler-owned interpreter layout.
func managedAgentEnvironment(project, python string) (string, bool) {
	root := filepath.Join(project, ".harnest", "environments")
	environment := filepath.Dir(filepath.Dir(python))
	name := filepath.Base(environment)
	managed := runtimePythonPath(environment) == python &&
		pathWithinDirectory(root, environment) &&
		environmentFingerprintPattern.MatchString(name)
	return name, managed
}

// ensureRegularLeaseDirectory rejects links throughout the private lease path.
func ensureRegularLeaseDirectory(path string) error {
	root := filepath.Dir(path)
	if err := ensureRegularPrivateDirectory(root, "agent environment lease root"); err != nil {
		return fmt.Errorf("prepare agent environment leases: %w", err)
	}
	if err := os.Mkdir(path, 0o755); err != nil && !os.IsExist(err) {
		return fmt.Errorf("create agent environment lease directory: %w", err)
	}
	info, err := os.Lstat(path)
	if err != nil {
		return fmt.Errorf("inspect agent environment lease directory: %w", err)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return fmt.Errorf("agent environment lease path must be a regular directory: %s", path)
	}
	return nil
}

// pruneAgentEnvironmentsAsync reclaims unleased fingerprints outside command latency.
func pruneAgentEnvironmentsAsync(project string) {
	stale := staleAgentEnvironments(project)
	if len(stale) == 0 {
		return
	}
	go func() {
		for _, path := range stale {
			_ = os.RemoveAll(path)
		}
	}()
}

// staleAgentEnvironments preserves the published and every actively leased runtime.
func staleAgentEnvironments(project string) []string {
	root := filepath.Join(project, ".harnest")
	current, safe := currentAgentEnvironments(root)
	if !safe {
		// A missing or malformed publication pointer cannot authorize deletion.
		return nil
	}
	environments := filepath.Join(root, "environments")
	entries, err := os.ReadDir(environments)
	if err != nil {
		return nil
	}
	stale := make([]string, 0, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if _, published := current[name]; published || !environmentFingerprintPattern.MatchString(name) {
			continue
		}
		if !unleasedEnvironment(root, entry) {
			continue
		}
		stale = append(stale, filepath.Join(environments, name))
	}
	return stale
}

// currentAgentEnvironments reads every valid profile pointer before authorizing deletion.
func currentAgentEnvironments(root string) (map[string]struct{}, bool) {
	current := make(map[string]struct{})
	for _, profile := range environmentProfiles {
		path := filepath.Join(root, profile.stateFile())
		contents, err := os.ReadFile(path)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return nil, false
		}
		name := environmentStateDirectory(contents)
		if name == "" {
			return nil, false
		}
		current[name] = struct{}{}
	}
	return current, len(current) > 0
}

// environmentStateDirectory validates one published relative fingerprint pointer.
func environmentStateDirectory(contents []byte) string {
	var state environmentState
	if json.Unmarshal(contents, &state) != nil {
		return ""
	}
	directory := filepath.Clean(filepath.FromSlash(state.Directory))
	if filepath.Dir(directory) != "environments" {
		return ""
	}
	name := filepath.Base(directory)
	if !environmentFingerprintPattern.MatchString(name) {
		return ""
	}
	return name
}

// unleasedEnvironment admits only regular cache directories with no live lease files.
func unleasedEnvironment(root string, entry os.DirEntry) bool {
	info, err := entry.Info()
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return false
	}
	leases := filepath.Join(root, environmentLeaseDirectory, entry.Name())
	leaseEntries, err := os.ReadDir(leases)
	return os.IsNotExist(err) || (err == nil && len(leaseEntries) == 0)
}
