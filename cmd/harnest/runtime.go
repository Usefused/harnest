package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/internal/runtimewheel"
)

type runtimeInstallOptions struct {
	directory       string
	bootstrapPython string
}

// Versioned commands come first because macOS may keep an older system
// python3 ahead of a supported package-manager installation on PATH.
var bootstrapPythonCandidates = []string{
	"python3.14",
	"python3.13",
	"python3.12",
	"python3.11",
	"python3.10",
	"python3",
	"python",
}

func (a *application) newRuntimeCommand() *cobra.Command {
	command := &cobra.Command{
		Use:   "runtime",
		Short: "Manage the Python runtime embedded in the Harnest CLI",
	}
	command.AddCommand(a.newRuntimeInstallCommand())
	return command
}

func (a *application) newRuntimeInstallCommand() *cobra.Command {
	options := runtimeInstallOptions{
		bootstrapPython: strings.TrimSpace(a.system.getenv("HARNEST_BOOTSTRAP_PYTHON")),
	}
	command := &cobra.Command{
		Use:   "install",
		Short: "Install the embedded Python runtime into a managed environment",
		Args:  cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			directory, err := a.runtimeDirectory(options.directory)
			if err != nil {
				return err
			}
			artifact, err := a.system.embeddedWheel(a.version)
			if err != nil {
				return fmt.Errorf("load embedded Python runtime: %w", err)
			}
			if err := a.installRuntime(command, directory, options.bootstrapPython, artifact); err != nil {
				return err
			}
			fmt.Fprintf(command.OutOrStdout(), "Installed embedded Harnest runtime %s at %s\n", a.version, directory)
			return nil
		},
	}
	command.Flags().StringVar(
		&options.directory,
		"directory",
		"",
		"managed runtime directory (default: HARNEST_RUNTIME_DIR or ~/.harnest/runtime)",
	)
	command.Flags().StringVar(
		&options.bootstrapPython,
		"bootstrap-python",
		options.bootstrapPython,
		"Python 3.10+ executable used to create the managed environment (default: auto-detect)",
	)
	return command
}

func (a *application) installRuntime(
	command *cobra.Command,
	directory string,
	bootstrapPython string,
	artifact runtimewheel.Artifact,
) error {
	bootstrap, err := a.resolveBootstrapPython(command.Context(), bootstrapPython)
	if err != nil {
		return err
	}
	wheelPath, cleanup, err := stageRuntimeWheel(artifact)
	if err != nil {
		return err
	}
	defer cleanup()
	if err := a.runRuntimeCommand(command, bootstrap, "-m", "venv", directory); err != nil {
		return fmt.Errorf("create managed Python environment: %w", err)
	}
	python := runtimePythonPath(directory)
	if err := a.runRuntimeCommand(
		command,
		python,
		"-m", "pip", "--disable-pip-version-check", "install", "--upgrade", wheelPath+"[all]",
	); err != nil {
		return fmt.Errorf("install embedded Harnest runtime: %w", err)
	}
	return nil
}

func (a *application) resolveBootstrapPython(ctx context.Context, requested string) (string, error) {
	if requested != "" {
		return a.resolveRequestedBootstrapPython(ctx, requested)
	}
	problems := make([]string, 0, len(bootstrapPythonCandidates))
	seen := make(map[string]struct{})
	for _, candidate := range bootstrapPythonCandidates {
		executable, err := a.system.lookPath(candidate)
		if err != nil || executableAlreadyChecked(seen, executable) {
			continue
		}
		if err := a.validateBootstrapPython(ctx, executable); err == nil {
			return executable, nil
		} else {
			problems = append(problems, err.Error())
		}
	}
	return "", unsupportedPythonError(problems)
}

func (a *application) resolveRequestedBootstrapPython(ctx context.Context, requested string) (string, error) {
	executable, err := a.system.lookPath(requested)
	if err != nil {
		return "", fmt.Errorf("bootstrap Python executable %q is unavailable: %w", requested, err)
	}
	if err := a.validateBootstrapPython(ctx, executable); err != nil {
		return "", err
	}
	return executable, nil
}

func executableAlreadyChecked(seen map[string]struct{}, executable string) bool {
	if _, exists := seen[executable]; exists {
		return true
	}
	seen[executable] = struct{}{}
	return false
}

func unsupportedPythonError(problems []string) error {
	detail := "no Python executable was found on PATH"
	if len(problems) != 0 {
		detail = strings.Join(problems, "; ")
	}
	return fmt.Errorf(
		"Python 3.10 or newer was not found: %s; install a supported Python or set HARNEST_BOOTSTRAP_PYTHON",
		detail,
	)
}

func (a *application) validateBootstrapPython(ctx context.Context, executable string) error {
	process := a.system.commandContext(
		ctx,
		executable,
		"-c",
		"import platform, sys; print(platform.python_version()); raise SystemExit(0 if sys.version_info >= (3, 10) else 1)",
	)
	var stdout bytes.Buffer
	process.Stdout = &stdout
	process.Stderr = io.Discard
	if err := process.Run(); err != nil {
		version := strings.TrimSpace(stdout.String())
		if version != "" {
			return fmt.Errorf("Python %s at %s is unsupported", version, executable)
		}
		return fmt.Errorf("inspect Python at %s: %w", executable, err)
	}
	return nil
}

func (a *application) runRuntimeCommand(command *cobra.Command, executable string, arguments ...string) error {
	process := a.system.commandContext(command.Context(), executable, arguments...)
	return runCommand(
		process,
		command.InOrStdin(),
		command.OutOrStdout(),
		command.ErrOrStderr(),
	)
}

func stageRuntimeWheel(artifact runtimewheel.Artifact) (string, func(), error) {
	directory, err := os.MkdirTemp("", "harnest-embedded-runtime-")
	if err != nil {
		return "", nil, fmt.Errorf("create embedded runtime staging directory: %w", err)
	}
	cleanup := func() { _ = os.RemoveAll(directory) }
	wheel := filepath.Join(directory, filepath.Base(artifact.Name))
	if err := os.WriteFile(wheel, artifact.Contents, 0o600); err != nil {
		cleanup()
		return "", nil, fmt.Errorf("stage embedded Harnest wheel: %w", err)
	}
	return wheel, cleanup, nil
}

func runtimePythonPath(directory string) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(directory, "Scripts", "python.exe")
	}
	return filepath.Join(directory, "bin", "python")
}
