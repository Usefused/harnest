package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"harnest.dev/harnest/engine"
	"harnest.dev/harnest/internal/runtimewheel"
)

// environmentBusyError prevents source-build fallback from bypassing a live lease.
type environmentBusyError struct{ message string }

func (e *environmentBusyError) Error() string { return e.message }

// managedEnvironmentName recognizes reusable slots and legacy fingerprint directories.
func managedEnvironmentName(name string) bool {
	return slotMatches("agent", name) || slotMatches("eval", name) || environmentFingerprintPattern.MatchString(name)
}

// slotMatches recognizes both managed generations of one dependency profile.
func slotMatches(slot, name string) bool {
	return name == slot || name == slot+"-next"
}

// replacementSlot uses one bounded alternate when a running command owns the primary.
// The caller holds the project lock; the final lease check precedes any mutation.
func replacementSlot(root, slot string) (string, error) {
	leases, err := os.ReadDir(filepath.Join(root, environmentLeaseDirectory, slot))
	if os.IsNotExist(err) || (err == nil && len(leases) == 0) {
		return slot, nil
	}
	if err != nil {
		return "", err
	}
	return slot + "-next", nil
}

// prepareEnvironmentReplacement refuses to mutate dependencies used by another command.
func prepareEnvironmentReplacement(paths environmentPaths, staged stagedAgentEnvironment) error {
	entries, err := os.ReadDir(filepath.Join(paths.root, "environments"))
	if err != nil {
		return err
	}
	for _, entry := range entries {
		// Older CLIs lease fingerprint directories that migration also needs to retire.
		if entry.Name() != filepath.Base(staged.directory) && !environmentFingerprintPattern.MatchString(entry.Name()) {
			continue
		}
		if !unleasedEnvironment(paths.root, entry) {
			return &environmentBusyError{message: fmt.Sprintf("agent environment %s is in use; stop running Harnest commands (including serve --reload) before changing dependencies, then retry", entry.Name())}
		}
	}
	// A failed install into another directory must preserve the serving publication.
	data, err := readRegularDependencyFile(paths.state)
	if err == nil && environmentStateDirectory(data) != filepath.Base(staged.directory) {
		return nil
	}
	if err := os.Remove(paths.state); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("invalidate agent environment: %w", err)
	}
	return nil
}

// migrateAgentEnvironments retires legacy pointers and caches after a successful sync.
// The caller holds environment.lock; user-owned directories and links are never removed.
func migrateAgentEnvironments(project string, selection pythonSelection) error {
	root := filepath.Join(project, ".harnest")
	if err := followSelectedSlot(project, root, selection); err != nil {
		return err
	}
	if err := removeLegacyEnvironmentStates(root); err != nil {
		return err
	}
	entries, err := os.ReadDir(filepath.Join(root, "environments"))
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if !environmentFingerprintPattern.MatchString(entry.Name()) {
			continue
		}
		if !unleasedEnvironment(root, entry) {
			continue
		}
		if err := os.RemoveAll(filepath.Join(root, "environments", entry.Name())); err != nil {
			return fmt.Errorf("remove obsolete agent environment: %w", err)
		}
	}
	return nil
}

// followSelectedSlot keeps an existing IDE link with its chosen dependency profile.
func followSelectedSlot(project, root string, selection pythonSelection) error {
	linked := linkedIDEEnvironment(root)
	selected := filepath.Base(filepath.Dir(filepath.Dir(selection.Executable)))
	// Follow a replacement only for the profile the editor already selected.
	if environmentFingerprintPattern.MatchString(linked) || (linked != selected && slotMatches(strings.TrimSuffix(selected, "-next"), linked)) {
		if _, err := syncIDEEnvironmentLink(project, selection.Executable); err != nil {
			return err
		}
	}
	return nil
}

// removeLegacyEnvironmentStates keeps only publications for the two reusable slots.
func removeLegacyEnvironmentStates(root string) error {
	for _, name := range []string{environmentStateFile, "environment-compile.json", "environment-development.json", "environment-eval.json"} {
		path := filepath.Join(root, name)
		contents, err := os.ReadFile(path)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return err
		}
		directory := environmentStateDirectory(contents)
		if (name == environmentStateFile && slotMatches("agent", directory)) || (name == "environment-eval.json" && slotMatches("eval", directory)) {
			continue
		}
		if err := os.Remove(path); err != nil {
			return fmt.Errorf("remove obsolete environment state: %w", err)
		}
	}
	return nil
}

// validateEnvironmentReplacement checks frozen inputs before invalidating a working interpreter.
func validateEnvironmentReplacement(
	bundle engine.Bundle, wheel runtimewheel.Artifact, plan runtimeDependencyPlan,
	profile environmentProfile, frozen bool, paths environmentPaths, staged stagedAgentEnvironment,
) error {
	if frozen {
		lockPath := filepath.Join(bundle.Directory, profile.requirementsLockFile())
		if err := validateFrozenRuntimeLock(bundle, wheel, plan, profile, lockPath); err != nil {
			return err
		}
	}
	// A failed sync must never leave a valid publication pointing at partial dependencies.
	return prepareEnvironmentReplacement(paths, staged)
}
