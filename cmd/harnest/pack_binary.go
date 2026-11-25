// Builds and loads standalone branded pack binaries that embed Harnest plus company pack payloads.

package main

import (
	"context"
	"crypto/sha256"
	"debug/macho"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/engine"
	"harnest.dev/harnest/internal/agentpack"
	"harnest.dev/harnest/internal/packbinary"
	"harnest.dev/harnest/internal/runtimewheel"
	"harnest.dev/harnest/internal/uvbootstrap"
)

const (
	packCoreInitEnv      = "HARNEST_PACK_CORE_INIT"
	packExtensionsEnv    = "HARNEST_PACK_EXTENSIONS"
	packCacheOverrideEnv = "HARNEST_PACK_CACHE"
	cliNameEnv           = "HARNEST_CLI_NAME"
	packWheelsLockMarker = "__HARNEST_PACK_WHEELS_URI__"
)

// sanitizeCLIName accepts only a valid kebab-case CLI token to block control-character injection from env vars.
func sanitizeCLIName(value string) string {
	trimmed := strings.TrimSpace(value)
	// Reject empty values or non-command tokens so untrusted environment overrides never corrupt stderr prefixes.
	if trimmed == "" || !scaffoldNamePattern.MatchString(trimmed) {
		return ""
	}
	return trimmed
}

// cliName returns the active branded command name, falling back to "harnest".
func (a *application) cliName() string {
	// Embedded pack binaries own their branded identity regardless of caller environment variables.
	if a.embeddedPack != nil && strings.TrimSpace(a.embeddedPack.Manifest.Command) != "" {
		return strings.TrimSpace(a.embeddedPack.Manifest.Command)
	}
	// Wheel-launched wrappers forward to harnest and propagate the branded command through HARNEST_CLI_NAME.
	if a.system.getenv != nil {
		if override := sanitizeCLIName(a.system.getenv(cliNameEnv)); override != "" {
			return override
		}
	}
	return "harnest"
}

// withCLINameEnv sets or replaces HARNEST_CLI_NAME in an environment slice without duplicating keys.
func withCLINameEnv(base []string, name string) []string {
	// Skip mutating the environment when running as unbranded harnest.
	if name == "" || name == "harnest" {
		return base
	}
	source := base
	// Inherit the current process environment when the caller passed nil so subprocesses keep PATH and credentials.
	if source == nil {
		source = os.Environ()
	}
	prefix := cliNameEnv + "="
	result := make([]string, 0, len(source)+1)
	for _, entry := range source {
		// Drop any inherited HARNEST_CLI_NAME so the active binary's branding wins deterministically.
		if strings.HasPrefix(entry, prefix) {
			continue
		}
		result = append(result, entry)
	}
	return append(result, prefix+name)
}

// loadEmbeddedPack checks the running executable for an appended pack payload and materializes it.
func loadEmbeddedPack(sys system) (*packbinary.LoadedPack, error) {
	if sys.embeddedPack != nil {
		return sys.embeddedPack, nil
	}
	executable, err := sys.executablePath()
	if err != nil {
		return nil, nil
	}
	cacheRoot, err := packBinaryCacheRoot(sys)
	if err != nil {
		return nil, err
	}
	return packbinary.Materialize(executable, cacheRoot)
}

// packBinaryCacheRoot selects a writable user cache directory for extracted pack payloads.
func packBinaryCacheRoot(sys system) (string, error) {
	if override := strings.TrimSpace(sys.getenv(packCacheOverrideEnv)); override != "" {
		return filepath.Abs(override)
	}
	return sys.userCacheDir()
}

// registerEmbeddedPackCommands rebrands the root CLI and attaches pack-defined subcommands.
func (a *application) registerEmbeddedPackCommands(root *cobra.Command) {
	if a.embeddedPack == nil {
		return
	}
	manifest := a.embeddedPack.Manifest
	root.Use = manifest.Command
	if strings.TrimSpace(manifest.Title) != "" {
		root.Short = manifest.Title
	}
	root.Version = fmt.Sprintf("%s (harnest %s)", manifest.Version, a.version)
	for _, entry := range manifest.Commands {
		cmd := entry
		root.AddCommand(&cobra.Command{
			Use:                cmd.Name,
			Short:              cmd.Help,
			DisableFlagParsing: true,
			RunE: func(command *cobra.Command, arguments []string) error {
				args := append([]string{"pack", "run", a.embeddedPack.PackDir(), cmd.Name}, arguments...)
				return a.runPackPython(command, args)
			},
		})
	}
}

// prependEmbeddedPack attaches the embedded pack folder once ahead of explicit --pack flags.
func (a *application) prependEmbeddedPack(packs []string, requireProjectHooks bool) []string {
	if a.embeddedPack == nil {
		return packs
	}
	if requireProjectHooks && !a.embeddedPack.Manifest.HasProjectHooks {
		return packs
	}
	embeddedDir := a.embeddedPack.PackDir()
	for _, existing := range packs {
		if abs, err := filepath.Abs(existing); err == nil && abs == embeddedDir {
			return packs
		}
	}
	return append([]string{embeddedDir}, packs...)
}

// packSubprocessEnv appends pack extension and CLI-branding variables for Python subprocesses.
func (a *application) packSubprocessEnv(base []string) []string {
	env := base
	// Attach embedded pack extensions when the standalone binary bundles them.
	if a.embeddedPack != nil && len(a.embeddedPack.Manifest.Extensions) > 0 {
		env = append(append([]string{}, env...), packExtensionsEnv+"="+a.embeddedPack.ExtensionsDir())
	}
	return withCLINameEnv(env, a.cliName())
}

// loadCompiledArtifact validates a compiled artifact while recognizing embedded pack extensions.
func (a *application) loadCompiledArtifact(artifact string, bundle engine.Bundle) (engine.CompiledArtifact, error) {
	if a.embeddedPack != nil && len(a.embeddedPack.Manifest.Extensions) > 0 {
		return engine.LoadCompiledArtifactWithPackExtensions(artifact, bundle, a.embeddedPack.ExtensionsDir())
	}
	return a.system.loadCompiledArtifact(artifact, bundle)
}

// packPython returns an interpreter with Harnest installed, bootstrapping from release assets when needed.
func (a *application) packPython(command *cobra.Command) (pythonSelection, error) {
	if strings.TrimSpace(a.pythonFlag) != "" || strings.TrimSpace(a.system.getenv("HARNEST_PYTHON")) != "" {
		return a.resolvePython()
	}
	wheel, err := a.system.embeddedWheel(a.version)
	if err != nil {
		// Contributor source builds lack an embedded wheel and fall back to host Python.
		if !releaseVersionPattern.MatchString(a.version) {
			return a.resolvePython()
		}
		return pythonSelection{}, fmt.Errorf("load bundled CLI runtime: %w", err)
	}
	directory, err := a.runtimeDirectory("")
	if err != nil {
		return pythonSelection{}, err
	}
	return a.preparePackCLIEnvironment(command, filepath.Join(filepath.Dir(directory), "cli"), wheel)
}

// preparePackCLIEnvironment caches a lightweight wheel-only CLI runtime for standalone pack binaries.
func (a *application) preparePackCLIEnvironment(
	command *cobra.Command, root string, wheel runtimewheel.Artifact,
) (pythonSelection, error) {
	if err := ensureRegularEnvironmentDirectory(root); err != nil {
		return pythonSelection{}, err
	}
	paths := environmentPaths{root: root, state: filepath.Join(root, "environment.json"), lock: filepath.Join(root, "install.lock")}
	fingerprint := fmt.Sprintf("%x", sha256.Sum256(wheel.Contents))
	unlock, err := lockEnvironment(paths.lock)
	if err != nil {
		return pythonSelection{}, fmt.Errorf("prepare CLI runtime: %w", err)
	}
	defer unlock()
	if selected, found := cachedAgentPython(paths, fingerprint); found {
		selected.Source = "CLI runtime"
		return selected, nil
	}
	relative := environmentRelativePath(fingerprint)
	directory := filepath.Join(root, filepath.FromSlash(relative))
	if err := ensureRegularEnvironmentDirectory(directory); err != nil {
		return pythonSelection{}, err
	}
	if err := a.installPackCLIEnvironment(command, directory, wheel); err != nil {
		return pythonSelection{}, err
	}
	if err := writeEnvironmentState(paths.state, environmentState{Fingerprint: fingerprint, Directory: relative}); err != nil {
		return pythonSelection{}, err
	}
	return pythonSelection{Executable: runtimePythonPath(directory), Source: "CLI runtime"}, nil
}

// installPackCLIEnvironment installs only the embedded Harnest wheel using embedded uv.
func (a *application) installPackCLIEnvironment(
	command *cobra.Command, directory string, wheel runtimewheel.Artifact,
) error {
	target, err := absoluteRuntimeTarget(directory)
	// Resolve relative paths before setting process.Dir so uv creates the CLI venv at the intended location.
	if err != nil {
		return err
	}
	artifact, err := a.system.embeddedUV()
	// Abort bootstrap if the embedded uv binary is unavailable.
	if err != nil {
		return fmt.Errorf("load CLI Python bootstrap: %w", err)
	}
	uv, cleanupUV, err := stageUV(artifact)
	// Abort if embedded uv cannot be written to an executable temp path.
	if err != nil {
		return err
	}
	defer cleanupUV()
	wheelPath, cleanupWheel, err := stageRuntimeWheel(wheel)
	// Abort if the embedded Harnest wheel cannot be staged for installation.
	if err != nil {
		return err
	}
	defer cleanupWheel()
	environment := mergedEnvironment(map[string]string{"UV_NO_CONFIG": "1", "UV_NO_PROGRESS": "1"})
	workingDirectory := runtimeCommandWorkingDirectory(target, uv)
	// Prefer any supported system Python (3.11-3.14) before falling back to a uv-managed 3.14 download;
	// pin process.Dir outside target so --clear never deletes uv's CWD and unlinked shell CWDs cannot fail bootstrap.
	if err := a.runRuntimeCommandInDirectory(command, workingDirectory, environment, uv, "venv", "--python", uvbootstrap.SupportedPythonSpec, "--python-preference", "system", "--clear", target); err != nil {
		return fmt.Errorf("create CLI runtime environment: %w", err)
	}
	python := runtimePythonPath(target)
	// Run pip install in the same existing working directory to avoid inheriting an unlinked shell CWD.
	if err := a.runRuntimeCommandInDirectory(command, workingDirectory, environment, uv, "pip", "install", "--python", python, wheelPath); err != nil {
		return fmt.Errorf("install bundled CLI runtime: %w", err)
	}
	return os.Chmod(target, 0o700)
}

// buildPackBinary stages a validated pack payload in Python and stamps it onto a Harnest binary.
func (a *application) buildPackBinary(
	command *cobra.Command, packDir, outputDir, baseBinaryPath string,
) error {
	stage, err := os.MkdirTemp("", "harnest-pack-binary-stage-*")
	if err != nil {
		return fmt.Errorf("create pack binary staging directory: %w", err)
	}
	defer os.RemoveAll(stage)
	if err := a.preparePackBinaryStage(command, packDir, stage); err != nil {
		return err
	}
	baseBytes, err := a.loadPackBaseBinary(command.Context(), baseBinaryPath)
	if err != nil {
		return err
	}
	target, manifest, err := a.stampPackBinary(command, outputDir, baseBytes, stage)
	if err != nil {
		return err
	}
	fmt.Fprintf(
		command.OutOrStdout(),
		"Built %s\nCommand: %s (%s %s)\nDigest: %s\nSkills: %d, Wheels: %d, Requirements: %d, Extensions: %d\n",
		target, manifest.Command, manifest.Name, manifest.Version, manifest.Digest,
		len(manifest.Skills), len(manifest.Wheels), len(manifest.Requirements), len(manifest.Extensions),
	)
	return nil
}

// preparePackBinaryStage runs Python validation, wheel builds, and deterministic payload staging.
func (a *application) preparePackBinaryStage(command *cobra.Command, packDir, stage string) error {
	args := []string{"pack", "prepare-binary", packDir, "--staging", stage}
	if uv, cleanup, err := a.packUV(); err == nil && uv != "" {
		defer cleanup()
		args = append(args, "--uv", uv)
	}
	python, err := a.packPython(command)
	if err != nil {
		return err
	}
	var stdout strings.Builder
	err = runPythonCLI(
		command.Context(), a, python, args, nil,
		command.InOrStdin(), &stdout, command.ErrOrStderr(),
	)
	if err != nil {
		return err
	}
	// Verify Python produced a parseable manifest before stamping the executable.
	var check map[string]any
	if err := json.Unmarshal([]byte(stdout.String()), &check); err != nil {
		return fmt.Errorf("read staged pack binary summary: %w", err)
	}
	return nil
}

// loadPackBaseBinary reads the requested base executable (or self) and removes any Mach-O signature.
func (a *application) loadPackBaseBinary(ctx context.Context, baseBinaryPath string) ([]byte, error) {
	source := strings.TrimSpace(baseBinaryPath)
	if source == "" {
		var err error
		source, err = a.system.executablePath()
		if err != nil {
			return nil, fmt.Errorf("resolve Harnest executable: %w", err)
		}
	}
	data, err := os.ReadFile(source)
	if err != nil {
		return nil, fmt.Errorf("read base binary %s: %w", source, err)
	}
	return stripMachOSignatureBytes(ctx, data)
}

// stripMachOSignatureBytes strips an existing Mach-O code signature before appending a payload.
func stripMachOSignatureBytes(ctx context.Context, data []byte) ([]byte, error) {
	if !agentpack.HasMachOSignature(data) {
		return data, nil
	}
	file, err := os.CreateTemp("", "harnest-pack-base-*")
	if err != nil {
		return nil, err
	}
	defer os.Remove(file.Name())
	if _, err := file.Write(data); err != nil {
		_ = file.Close()
		return nil, err
	}
	if err := file.Close(); err != nil {
		return nil, err
	}
	process := exec.CommandContext(ctx, "codesign", "--remove-signature", file.Name())
	if output, err := process.CombinedOutput(); err != nil {
		return nil, fmt.Errorf("prepare base binary signature: %w: %s", err, output)
	}
	return os.ReadFile(file.Name())
}

// stampPackBinary writes the standalone executable and refreshes macOS ad-hoc signing when needed.
func (a *application) stampPackBinary(
	command *cobra.Command, outputDir string, baseBytes []byte, stage string,
) (string, packbinary.Manifest, error) {
	manifestData, err := os.ReadFile(filepath.Join(stage, packbinary.ManifestFile))
	if err != nil {
		return "", packbinary.Manifest{}, err
	}
	var peek packbinary.Manifest
	if err := json.Unmarshal(manifestData, &peek); err != nil {
		return "", packbinary.Manifest{}, err
	}
	if err := peek.Validate(); err != nil {
		return "", packbinary.Manifest{}, err
	}
	filename := peek.Command
	if packbinary.IsWindowsPE(baseBytes) {
		filename += ".exe"
	}
	target := filepath.Join(outputDir, filename)
	manifest, err := packbinary.WriteBinary(target, baseBytes, stage)
	if err != nil {
		return "", packbinary.Manifest{}, err
	}
	if err := a.signStampedPackBinary(command, target); err != nil {
		return "", packbinary.Manifest{}, err
	}
	return target, manifest, nil
}

// signStampedPackBinary re-signs Mach-O executables on macOS after extending __LINKEDIT.
func (a *application) signStampedPackBinary(command *cobra.Command, target string) error {
	if runtime.GOOS != "darwin" {
		return nil
	}
	file, err := macho.Open(target)
	if err != nil {
		// Cross-stamped ELF/PE binaries or synthetic test stubs are not Mach-O images.
		return nil
	}
	_ = file.Close()
	return signAgentExecutable(command, a, target)
}
