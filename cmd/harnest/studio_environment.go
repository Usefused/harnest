package main

import (
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"
	"harnest.dev/harnest/internal/runtimewheel"
	"harnest.dev/harnest/internal/uvbootstrap"
)

// studioPython keeps Studio's framework independent of each authored agent's runtime.
func (a *application) studioPython(command *cobra.Command) (pythonSelection, error) {
	if strings.TrimSpace(a.pythonFlag) != "" || strings.TrimSpace(a.system.getenv("HARNEST_PYTHON")) != "" {
		return a.resolvePython()
	}
	wheel, err := a.system.embeddedWheel(a.version)
	if err != nil {
		if !releaseVersionPattern.MatchString(a.version) {
			return a.resolvePython()
		}
		return pythonSelection{}, fmt.Errorf("load bundled Studio runtime: %w", err)
	}
	directory, err := a.runtimeDirectory("")
	if err != nil {
		return pythonSelection{}, err
	}
	return a.prepareStudioEnvironment(command, filepath.Join(filepath.Dir(directory), "studio"), wheel)
}

// prepareStudioEnvironment reuses only a completely installed wheel-specific environment.
func (a *application) prepareStudioEnvironment(command *cobra.Command, root string, wheel runtimewheel.Artifact) (pythonSelection, error) {
	if err := ensureRegularEnvironmentDirectory(root); err != nil {
		return pythonSelection{}, err
	}
	paths := environmentPaths{root: root, state: filepath.Join(root, "environment.json"), lock: filepath.Join(root, "install.lock")}
	fingerprint := fmt.Sprintf("%x", sha256.Sum256(wheel.Contents))
	unlock, err := lockEnvironment(paths.lock)
	if err != nil {
		return pythonSelection{}, fmt.Errorf("prepare Studio: %w", err)
	}
	defer unlock()
	if selected, found := cachedAgentPython(paths, fingerprint); found {
		selected.Source = "Studio environment"
		return selected, nil
	}
	relative := environmentRelativePath(fingerprint)
	directory := filepath.Join(root, filepath.FromSlash(relative))
	if err := ensureRegularEnvironmentDirectory(directory); err != nil {
		return pythonSelection{}, err
	}
	fmt.Fprintln(command.OutOrStdout(), "Preparing the bundled Studio runtime (first launch of this release)…")
	if err := a.installStudioEnvironment(command, directory, wheel); err != nil {
		return pythonSelection{}, err
	}
	if err := writeEnvironmentState(paths.state, environmentState{Fingerprint: fingerprint, Directory: relative}); err != nil {
		return pythonSelection{}, err
	}
	return pythonSelection{Executable: runtimePythonPath(directory), Source: "Studio environment"}, nil
}

// installStudioEnvironment stages release assets and publishes no cache marker on partial failure.
func (a *application) installStudioEnvironment(command *cobra.Command, directory string, wheel runtimewheel.Artifact) error {
	artifact, err := a.system.embeddedUV()
	if err != nil {
		return fmt.Errorf("load Studio Python bootstrap: %w", err)
	}
	uv, cleanupUV, err := stageUV(artifact)
	if err != nil {
		return err
	}
	defer cleanupUV()
	wheelPath, cleanupWheel, err := stageRuntimeWheel(wheel)
	if err != nil {
		return err
	}
	defer cleanupWheel()
	environment := mergedEnvironment(map[string]string{"UV_NO_CONFIG": "1", "UV_NO_PROGRESS": "1"})
	if err := a.runRuntimeCommandWithEnvironment(command, environment, uv, "venv", "--python", uvbootstrap.ManagedPythonVersion, "--managed-python", "--clear", directory); err != nil {
		return fmt.Errorf("create Studio environment: %w", err)
	}
	python := runtimePythonPath(directory)
	if err := a.runRuntimeCommandWithEnvironment(command, environment, uv, "pip", "install", "--python", python, wheelPath+"[studio]"); err != nil {
		return fmt.Errorf("install bundled Studio: %w", err)
	}
	// A wheel missing browser assets or the compiled server is never cached as ready.
	check := "from pathlib import Path; import harnest_builder.app as a; from harnest_builder.assistant_build import PACKAGE; assert all((a.STATIC / p).is_file() for p in a.ASSETS); assert (PACKAGE / '_assistant' / 'harnest-manifest.json').is_file()"
	if err := a.runRuntimeCommandWithEnvironment(command, environment, python, "-I", "-c", check); err != nil {
		return fmt.Errorf("verify bundled Studio: %w", err)
	}
	// Keep the cache owned by this user; agent workspaces remain separate.
	return os.Chmod(directory, 0o700)
}
