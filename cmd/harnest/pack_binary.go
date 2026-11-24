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
	packWheelsLockMarker = "__HARNEST_PACK_WHEELS_URI__"
)

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

// packSubprocessEnv appends HARNEST_PACK_EXTENSIONS when the binary owns embedded extensions.
func (a *application) packSubprocessEnv(base []string) []string {
	if a.embeddedPack == nil || len(a.embeddedPack.Manifest.Extensions) == 0 {
		return base
	}
	return append(append([]string{}, base...), packExtensionsEnv+"="+a.embeddedPack.ExtensionsDir())
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
	artifact, err := a.system.embeddedUV()
	if err != nil {
		return fmt.Errorf("load CLI Python bootstrap: %w", err)
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
	// Prefer any supported system Python (3.11-3.14) before falling back to a uv-managed 3.14 download.
	if err := a.runRuntimeCommandWithEnvironment(command, environment, uv, "venv", "--python", uvbootstrap.SupportedPythonSpec, "--python-preference", "system", "--clear", directory); err != nil {
		return fmt.Errorf("create CLI runtime environment: %w", err)
	}
	python := runtimePythonPath(directory)
	if err := a.runRuntimeCommandWithEnvironment(command, environment, uv, "pip", "install", "--python", python, wheelPath); err != nil {
		return fmt.Errorf("install bundled CLI runtime: %w", err)
	}
	return os.Chmod(directory, 0o700)
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
