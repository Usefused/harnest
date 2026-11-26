// `harnest runtime`: installs the CLI's Python runtime; parent of `harnest runtime build`.

package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/internal/runtimewheel"
	"harnest.dev/harnest/internal/uvbootstrap"
)

type runtimeInstallOptions struct {
	directory       string
	bootstrapPython string
}

type runtimeBootstrapError struct {
	cause error
}

func (e runtimeBootstrapError) Error() string { return e.cause.Error() }
func (e runtimeBootstrapError) Unwrap() error { return e.cause }

// Versioned commands come first because macOS may keep an older system
// python3 ahead of a supported package-manager installation on PATH.
var bootstrapPythonCandidates = []string{
	"python3.14",
	"python3.13",
	"python3.12",
	"python3.11",
	"python3",
	"python",
}

// newRuntimeCommand separates CLI bootstrap installation from portable agent runtime builds.
func (a *application) newRuntimeCommand() *cobra.Command {
	command := &cobra.Command{
		Use:   "runtime",
		Short: "Manage Python environments and portable agent runtime packs",
	}
	command.AddCommand(a.newRuntimeInstallCommand(), a.newRuntimeBuildCommand())
	return command
}

// newRuntimeInstallCommand configures installation with a supported bootstrap Python.
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
		"Python 3.11+ executable used instead of Harnest-managed Python",
	)
	return command
}

func (a *application) installRuntime(
	command *cobra.Command,
	directory string,
	bootstrapPython string,
	artifact runtimewheel.Artifact,
) error {
	wheelPath, cleanup, err := stageRuntimeWheel(artifact)
	if err != nil {
		return err
	}
	defer cleanup()
	bootstrap, discoveryErr := a.resolveBootstrapPython(command.Context(), bootstrapPython)
	if discoveryErr != nil && bootstrapPython == "" {
		return a.installRuntimeWithManagedPython(command, directory, wheelPath, discoveryErr)
	}
	if discoveryErr != nil {
		return discoveryErr
	}
	installErr := a.installRuntimeWithPython(command, directory, bootstrap, wheelPath)
	if bootstrapPython == "" && isRuntimeBootstrapError(installErr) {
		// A version-compatible interpreter can still lack venv/ensurepip support;
		// the managed path keeps that host packaging detail out of the user contract.
		return a.installRuntimeWithManagedPython(command, directory, wheelPath, installErr)
	}
	return installErr
}

// installRuntimeWithPython creates and populates the managed runtime using a host Python interpreter.
func (a *application) installRuntimeWithPython(
	command *cobra.Command,
	directory, bootstrap, wheelPath string,
) error {
	target, err := absoluteRuntimeTarget(directory)
	// Fail early if a relative target directory cannot be resolved before pinning subprocess Dir.
	if err != nil {
		return err
	}
	// Prefer system or embedded uv with the host Python so UV_INDEX_URL, UV_DEFAULT_INDEX, and uv.toml are honored.
	if uvPath, cleanupUV, uvErr := a.resolveUV(); uvErr == nil {
		defer cleanupUV()
		return a.installRuntimeWithUV(command, target, uvPath, bootstrap, wheelPath, false)
	}
	workingDirectory := runtimeCommandWorkingDirectory(target, wheelPath)
	// Fall back to stdlib venv + pip only when neither system nor embedded uv is available.
	if err := a.runRuntimeCommandInDirectory(command, workingDirectory, nil, bootstrap, "-m", "venv", target); err != nil {
		return runtimeBootstrapError{cause: fmt.Errorf("create managed Python environment: %w", err)}
	}
	python := runtimePythonPath(target)
	// Keep pip install pinned to the same stable working directory while installing the staged wheel.
	if err := a.runRuntimeCommandInDirectory(
		command,
		workingDirectory,
		nil,
		python,
		"-m", "pip", "--disable-pip-version-check", "install", "--upgrade", wheelPath,
	); err != nil {
		return fmt.Errorf("install embedded Harnest runtime: %w", err)
	}
	return nil
}

func isRuntimeBootstrapError(err error) bool {
	var bootstrapErr runtimeBootstrapError
	return errors.As(err, &bootstrapErr)
}

// installRuntimeWithManagedPython bootstraps a managed Python interpreter and Harnest wheel via system or embedded uv.
func (a *application) installRuntimeWithManagedPython(
	command *cobra.Command,
	directory, wheelPath string,
	discoveryErr error,
) error {
	target, err := absoluteRuntimeTarget(directory)
	// Resolve relative targets before changing process.Dir so uv creates the environment at the requested location.
	if err != nil {
		return err
	}
	uvPath, embeddedUV, cleanup, err := a.resolveUVWithOrigin()
	// Surface missing uv alongside the earlier host-Python discovery failure.
	if err != nil {
		return fmt.Errorf("bootstrap managed Python after %v: %w", discoveryErr, err)
	}
	defer cleanup()
	// Report accurately whether managed Python is being installed via system uv or embedded uv.
	if embeddedUV {
		fmt.Fprintf(
			command.OutOrStdout(),
			"No compatible system Python found; installing managed Python %s with embedded uv %s.\n",
			uvbootstrap.ManagedPythonVersion,
			uvbootstrap.Version,
		)
	} else {
		fmt.Fprintf(
			command.OutOrStdout(),
			"No compatible system Python found; installing managed Python %s with system uv (%s).\n",
			uvbootstrap.ManagedPythonVersion,
			uvPath,
		)
	}
	return a.installRuntimeWithUV(command, target, uvPath, uvbootstrap.ManagedPythonVersion, wheelPath, true)
}

// installRuntimeWithUV creates the runtime venv and installs the embedded wheel using the selected uv binary.
func (a *application) installRuntimeWithUV(
	command *cobra.Command,
	target, uvPath, pythonSpec, wheelPath string,
	managedOnly bool,
) error {
	environment := a.uvEnvironment(map[string]string{
		"UV_PYTHON_INSTALL_DIR": managedPythonDirectory(target),
	})
	workingDirectory := runtimeCommandWorkingDirectory(target, uvPath)
	venvArgs := []string{"venv", "--python", pythonSpec}
	// Request uv-managed CPython explicitly when no compatible system interpreter was found.
	if managedOnly {
		venvArgs = append(venvArgs, "--managed-python")
	}
	venvArgs = append(venvArgs, "--clear", target)
	// Pin uv's working directory outside target so --clear cannot unlink uv's own CWD and stale shell CWDs never fail bootstrap.
	if err := a.runRuntimeCommandInDirectory(command, workingDirectory, environment, uvPath, venvArgs...); err != nil {
		if managedOnly {
			return fmt.Errorf("install managed Python %s: %w", pythonSpec, err)
		}
		return runtimeBootstrapError{cause: fmt.Errorf("create managed Python environment: %w", err)}
	}
	// Install the staged wheel from the same verified working directory so uv never depends on the caller's shell CWD.
	if err := a.runRuntimeCommandInDirectory(
		command,
		workingDirectory,
		environment,
		uvPath,
		"pip", "install", "--python", runtimePythonPath(target), "--upgrade", wheelPath,
	); err != nil {
		return fmt.Errorf("install embedded Harnest runtime: %w", err)
	}
	return nil
}

// resolveUV prefers HARNEST_UV or a system-installed uv on PATH before staging the embedded uv binary.
func (a *application) resolveUV() (string, func(), error) {
	path, _, cleanup, err := a.resolveUVWithOrigin()
	return path, cleanup, err
}

// resolveUVWithOrigin resolves the preferred uv executable and reports whether it came from the embedded bundle.
func (a *application) resolveUVWithOrigin() (string, bool, func(), error) {
	// Honor an explicit HARNEST_UV override when configured by the user or corporate tooling.
	if override := strings.TrimSpace(a.system.getenv("HARNEST_UV")); override != "" {
		resolved, err := a.system.lookPath(override)
		// Fail loudly on an explicit HARNEST_UV override instead of silently falling back.
		if err != nil {
			return "", false, nil, fmt.Errorf("HARNEST_UV executable %q is unavailable: %w", override, err)
		}
		return resolved, false, func() {}, nil
	}
	// Prefer an already-installed system uv on PATH so host wrappers, certs, and caches are reused first.
	if executable, err := a.system.lookPath("uv"); err == nil && strings.TrimSpace(executable) != "" {
		return executable, false, func() {}, nil
	}
	// Fall back to the embedded uv binary bundled in released Harnest executables.
	artifact, err := a.system.embeddedUV()
	if err != nil {
		return "", false, nil, err
	}
	stagedPath, cleanup, err := stageUV(artifact)
	return stagedPath, true, cleanup, err
}

// uvEnvironment builds subprocess env vars for uv, preserving user uv.toml and bridging legacy PIP_INDEX_URL.
func (a *application) uvEnvironment(overrides map[string]string) []string {
	merged := make(map[string]string, len(overrides)+2)
	for key, value := range overrides {
		merged[key] = value
	}
	merged["UV_NO_PROGRESS"] = "1"
	// Bridge legacy PIP_INDEX_URL to UV_DEFAULT_INDEX only when no explicit UV index env var is set.
	if a.shouldBridgePIPIndex(merged) {
		merged["UV_DEFAULT_INDEX"] = strings.TrimSpace(a.system.getenv("PIP_INDEX_URL"))
	}
	return mergedEnvironment(merged)
}

// shouldBridgePIPIndex reports whether PIP_INDEX_URL should populate UV_DEFAULT_INDEX for uv commands.
func (a *application) shouldBridgePIPIndex(overrides map[string]string) bool {
	if strings.TrimSpace(a.system.getenv("PIP_INDEX_URL")) == "" {
		return false
	}
	// Never override an explicit UV_DEFAULT_INDEX or UV_INDEX_URL from the caller or agent config.
	if strings.TrimSpace(a.system.getenv("UV_DEFAULT_INDEX")) != "" ||
		strings.TrimSpace(a.system.getenv("UV_INDEX_URL")) != "" {
		return false
	}
	return strings.TrimSpace(overrides["UV_DEFAULT_INDEX"]) == "" &&
		strings.TrimSpace(overrides["UV_INDEX_URL"]) == ""
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

// unsupportedPythonError explains the minimum version after discovery is exhausted.
func unsupportedPythonError(problems []string) error {
	detail := "no Python executable was found on PATH"
	if len(problems) != 0 {
		detail = strings.Join(problems, "; ")
	}
	return fmt.Errorf(
		"Python 3.11 or newer was not found: %s",
		detail,
	)
}

// validateBootstrapPython rejects hosts older than the runtime package supports.
func (a *application) validateBootstrapPython(ctx context.Context, executable string) error {
	process := a.system.commandContext(
		ctx,
		executable,
		"-c",
		"import platform, sys; print(platform.python_version()); raise SystemExit(0 if sys.version_info >= (3, 11) else 1)",
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

// runRuntimeCommandWithEnvironment runs a runtime subprocess with explicit environment variables.
func (a *application) runRuntimeCommandWithEnvironment(
	command *cobra.Command,
	environment []string,
	executable string,
	arguments ...string,
) error {
	return a.runRuntimeCommandInDirectory(command, "", environment, executable, arguments...)
}

// runRuntimeCommandInDirectory executes a runtime subprocess with optional working directory and environment overrides.
func (a *application) runRuntimeCommandInDirectory(
	command *cobra.Command,
	directory string,
	environment []string,
	executable string,
	arguments ...string,
) error {
	process := a.system.commandContext(command.Context(), executable, arguments...)
	// Setting process.Dir insulates uv and bootstrap interpreters from removed or unlinked caller CWDs.
	if directory != "" {
		process.Dir = directory
	}
	// Apply explicit environment variables only when provided so callers can inherit process defaults cleanly.
	if environment != nil {
		process.Env = environment
	}
	return runCommand(
		process,
		command.InOrStdin(),
		command.OutOrStdout(),
		command.ErrOrStderr(),
	)
}

// absoluteRuntimeTarget normalizes target paths without consulting os.Getwd when already absolute.
func absoluteRuntimeTarget(directory string) (string, error) {
	// Avoid filepath.Abs for absolute paths because os.Getwd fails when invoked from an unlinked shell directory.
	if filepath.IsAbs(directory) {
		return filepath.Clean(directory), nil
	}
	return filepath.Abs(directory)
}

// runtimeCommandWorkingDirectory chooses an existing directory outside targetDirectory so `uv venv --clear`
// never removes its own working directory and callers in unlinked shells never trigger missing-CWD errors.
func runtimeCommandWorkingDirectory(targetDirectory, stagedAssetPath string) string {
	cleanedTarget := filepath.Clean(targetDirectory)
	parent := filepath.Dir(cleanedTarget)
	// Prefer targetDirectory's parent when it exists and is distinct from the cleared target itself.
	if parent != "" && parent != cleanedTarget {
		// Verify parent exists on disk before using it because fresh runtime installs may not have created ~/.harnest yet.
		if info, err := os.Stat(parent); err == nil && info.IsDir() {
			return parent
		}
	}
	stagedDirectory := filepath.Dir(stagedAssetPath)
	// Fall back to the staged asset directory created by MkdirTemp when the target's parent does not exist yet.
	if stagedDirectory != "" {
		// Guard against synthetic test paths where stagedAssetPath may not live on disk.
		if info, err := os.Stat(stagedDirectory); err == nil && info.IsDir() {
			return stagedDirectory
		}
	}
	return os.TempDir()
}

func stageRuntimeWheel(artifact runtimewheel.Artifact) (string, func(), error) {
	return stageEmbeddedArtifact(
		"harnest-embedded-runtime-",
		artifact.Name,
		artifact.Contents,
		0o600,
	)
}

func stageUV(artifact uvbootstrap.Artifact) (string, func(), error) {
	return stageEmbeddedArtifact("harnest-embedded-uv-", artifact.Name, artifact.Contents, 0o700)
}

func stageEmbeddedArtifact(prefix, name string, contents []byte, mode os.FileMode) (string, func(), error) {
	directory, err := os.MkdirTemp("", prefix)
	if err != nil {
		return "", nil, fmt.Errorf("create embedded artifact staging directory: %w", err)
	}
	cleanup := func() { _ = os.RemoveAll(directory) }
	artifactPath := filepath.Join(directory, filepath.Base(name))
	if err := os.WriteFile(artifactPath, contents, mode); err != nil {
		cleanup()
		return "", nil, fmt.Errorf("stage embedded artifact: %w", err)
	}
	return artifactPath, cleanup, nil
}

func managedPythonDirectory(runtimeDirectory string) string {
	return filepath.Join(filepath.Dir(runtimeDirectory), "python")
}

// runtimePythonPath returns the interpreter inside a uv-created environment on this host.
func runtimePythonPath(directory string) string {
	return environmentPython(runtime.GOOS, directory)
}

// environmentPython follows uv's environment layout, which differs on Windows.
func environmentPython(goos, directory string) string {
	if goos == "windows" {
		return filepath.Join(directory, "Scripts", "python.exe")
	}
	return filepath.Join(directory, "bin", "python")
}
