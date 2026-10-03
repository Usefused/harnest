package main

import (
	"fmt"
	"os"
	"path/filepath"

	"harnest.dev/harnest/engine"
	"harnest.dev/harnest/internal/runtimewheel"
)

// environmentBusyError prevents source-build fallback from bypassing a live lease.
type environmentBusyError struct{ message string }

func (e *environmentBusyError) Error() string { return e.message }

// managedEnvironmentName recognizes reusable slots and legacy fingerprint directories.
func managedEnvironmentName(name string) bool {
	return name == "agent" || name == "eval" || environmentFingerprintPattern.MatchString(name)
}

// prepareEnvironmentReplacement refuses to mutate dependencies used by another command.
func prepareEnvironmentReplacement(paths environmentPaths, staged stagedAgentEnvironment) error {
	entries, err := os.ReadDir(filepath.Join(paths.root, "environments"))
	if err != nil {
		return err
	}
	for _, entry := range entries {
		// All legacy environments must be idle before migration can enforce the two-slot limit.
		if entry.Name() != filepath.Base(staged.directory) && !environmentFingerprintPattern.MatchString(entry.Name()) {
			continue
		}
		if !unleasedEnvironment(paths.root, entry) {
			return &environmentBusyError{message: fmt.Sprintf("agent environment %s is in use; stop running Harnest commands (including serve --reload) before changing dependencies, then retry", entry.Name())}
		}
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
	if environmentFingerprintPattern.MatchString(linkedIDEEnvironment(root)) {
		if _, err := syncIDEEnvironmentLink(project, selection.Executable); err != nil {
			return err
		}
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
		if (name == environmentStateFile && directory == "agent") || (name == "environment-eval.json" && directory == "eval") {
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
