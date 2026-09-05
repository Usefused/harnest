package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/engine"
	"harnest.dev/harnest/internal/runtimewheel"
	"harnest.dev/harnest/internal/uvbootstrap"
)

const environmentStateFile = "environment.json"

var releaseVersionPattern = regexp.MustCompile(`^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+].+)?$`)

type environmentState struct {
	Fingerprint string `json:"fingerprint"`
	Directory   string `json:"directory"`
}

func (a *application) newEnvironmentCommand() *cobra.Command {
	command := &cobra.Command{
		Use:   "env",
		Short: "Manage an agent's isolated Python environment",
	}
	command.AddCommand(a.newEnvironmentSyncCommand())
	return command
}

func (a *application) newEnvironmentSyncCommand() *cobra.Command {
	var frozen bool
	command := &cobra.Command{
		Use:   "sync AGENT_DIR",
		Short: "Synchronize the complete locked agent runtime",
		Args:  cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			bundle, err := loadAgentBundle(arguments[0])
			if err != nil {
				return err
			}
			selection, err := a.syncAgentEnvironment(command, bundle, frozen)
			if err != nil {
				return err
			}
			fmt.Fprintf(
				command.OutOrStdout(),
				"Agent environment ready: %s\n",
				selection.Executable,
			)
			return nil
		},
	}
	command.Flags().BoolVar(
		&frozen,
		"frozen",
		false,
		"require an existing current runtime lock without updating it",
	)
	return command
}

func (a *application) agentPython(
	command *cobra.Command, bundle engine.Bundle,
) (pythonSelection, error) {
	selection, err := a.syncAgentEnvironment(command, bundle, false)
	if err == nil {
		return selection, nil
	}
	if releaseVersionPattern.MatchString(a.version) {
		return pythonSelection{}, err
	}
	// Source builds intentionally lack release-embedded assets. Falling back
	// keeps contributor workflows usable without weakening released binaries.
	return a.resolvePython()
}

func (a *application) syncAgentEnvironment(
	command *cobra.Command,
	bundle engine.Bundle,
	frozen bool,
) (pythonSelection, error) {
	if err := validateAgentDependencyPolicy(bundle); err != nil {
		return pythonSelection{}, err
	}
	plan, err := inspectRuntimeDependencyPlan(bundle)
	if err != nil {
		return pythonSelection{}, err
	}
	wheel, err := a.system.embeddedWheel(a.version)
	if err != nil {
		return pythonSelection{}, fmt.Errorf("load embedded Harnest wheel: %w", err)
	}
	uv, err := a.system.embeddedUV()
	if err != nil {
		return pythonSelection{}, fmt.Errorf("load embedded uv: %w", err)
	}
	paths, err := inspectEnvironmentPaths(bundle)
	if err != nil {
		return pythonSelection{}, err
	}
	fingerprint, err := environmentFingerprint(bundle, wheel, plan)
	if err != nil {
		return pythonSelection{}, err
	}
	if selection, found := cachedAgentPython(paths, fingerprint); found {
		return selection, nil
	}
	unlock, err := lockEnvironment(paths.lock)
	if err != nil {
		return pythonSelection{}, err
	}
	defer unlock()
	if selection, found := cachedAgentPython(paths, fingerprint); found {
		return selection, nil
	}
	return a.installAgentEnvironment(command, bundle, wheel, uv, paths, plan, frozen)
}

type environmentPaths struct {
	root, state, lock string
}

func inspectEnvironmentPaths(bundle engine.Bundle) (environmentPaths, error) {
	root := filepath.Join(bundle.Directory, ".harnest")
	if err := ensureRegularEnvironmentDirectory(root); err != nil {
		return environmentPaths{}, err
	}
	environments := filepath.Join(root, "environments")
	if err := ensureRegularEnvironmentDirectory(environments); err != nil {
		return environmentPaths{}, err
	}
	return environmentPaths{
		root:  root,
		state: filepath.Join(root, environmentStateFile),
		lock:  filepath.Join(root, "environment.lock"),
	}, nil
}

func ensureRegularEnvironmentDirectory(path string) error {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		if err := os.MkdirAll(path, 0o755); err != nil {
			return fmt.Errorf("create agent environment directory %s: %w", path, err)
		}
		return nil
	}
	if err != nil {
		return fmt.Errorf("inspect agent environment directory %s: %w", path, err)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return fmt.Errorf("agent environment path must be a regular directory: %s", path)
	}
	return nil
}

// environmentFingerprint invalidates cached environments when committed dependency pins change.
func environmentFingerprint(
	bundle engine.Bundle, wheel runtimewheel.Artifact, plan runtimeDependencyPlan,
) (string, error) {
	digest := sha256.New()
	for _, value := range []string{
		bundle.Config.Spec.Runtime.Version,
		bundle.Config.Spec.Framework.Name,
		bundle.Config.Spec.Framework.EffectiveMode(),
		wheel.Name,
	} {
		digest.Write([]byte(value))
		digest.Write([]byte{0})
	}
	digest.Write(wheel.Contents)
	for _, path := range plan.ProjectFiles {
		if err := hashEnvironmentDependencyInput(digest, bundle.Directory, path, false); err != nil {
			return "", err
		}
	}
	for _, path := range []string{
		filepath.Join(bundle.Directory, "harnest.lock"),
		filepath.Join(bundle.Directory, runtimeRequirementsLockFile),
	} {
		if err := hashEnvironmentDependencyInput(digest, bundle.Directory, path, true); err != nil {
			return "", err
		}
	}
	// Task source contents do not change the dependency graph; presence alone
	// controls whether the compiler-owned queue runtime joins the environment.
	digest.Write([]byte{byte(0), byte(boolByte(plan.HasTasks))})
	return hex.EncodeToString(digest.Sum(nil)), nil
}

// hashEnvironmentDependencyInput binds both location and bounded file contents.
func hashEnvironmentDependencyInput(
	digest interface{ Write([]byte) (int, error) }, root, file string, optional bool,
) error {
	relative, err := filepath.Rel(root, file)
	if err != nil {
		return fmt.Errorf("resolve agent dependency input %s: %w", file, err)
	}
	contents, err := readRegularDependencyFile(file)
	if optional && os.IsNotExist(err) {
		digest.Write([]byte(filepath.ToSlash(relative)))
		digest.Write([]byte{0, 0})
		return nil
	}
	if err != nil {
		return fmt.Errorf("read agent dependency input %s: %w", relative, err)
	}
	digest.Write([]byte(filepath.ToSlash(relative)))
	digest.Write([]byte{0})
	digest.Write(contents)
	digest.Write([]byte{0})
	return nil
}

func boolByte(value bool) int {
	if value {
		return 1
	}
	return 0
}

func cachedAgentPython(
	paths environmentPaths, fingerprint string,
) (pythonSelection, bool) {
	if info, err := os.Lstat(paths.state); err != nil || info.Mode()&os.ModeSymlink != 0 {
		return pythonSelection{}, false
	}
	contents, err := os.ReadFile(paths.state)
	if err != nil {
		return pythonSelection{}, false
	}
	var state environmentState
	if json.Unmarshal(contents, &state) != nil || state.Fingerprint != fingerprint {
		return pythonSelection{}, false
	}
	directory, ok := containedEnvironmentDirectory(paths.root, state.Directory)
	if !ok {
		return pythonSelection{}, false
	}
	python := runtimePythonPath(directory)
	if info, err := os.Stat(python); err != nil || !info.Mode().IsRegular() {
		return pythonSelection{}, false
	}
	return pythonSelection{Executable: python, Source: "agent environment"}, true
}

func containedEnvironmentDirectory(root, relative string) (string, bool) {
	if filepath.IsAbs(relative) {
		return "", false
	}
	directory := filepath.Join(root, filepath.Clean(filepath.FromSlash(relative)))
	contained, err := filepath.Rel(root, directory)
	if err != nil || contained == ".." || strings.HasPrefix(contained, ".."+string(os.PathSeparator)) {
		return "", false
	}
	info, err := os.Lstat(directory)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return "", false
	}
	return directory, true
}

func lockEnvironment(path string) (func(), error) {
	if err := os.Mkdir(path, 0o755); err != nil {
		if os.IsExist(err) {
			return nil, fmt.Errorf("another agent environment sync is in progress: %s", path)
		}
		return nil, fmt.Errorf("lock agent environment: %w", err)
	}
	return func() { _ = os.Remove(path) }, nil
}

// installAgentEnvironment publishes only after the complete runtime lock is installed and verified.
func (a *application) installAgentEnvironment(
	command *cobra.Command,
	bundle engine.Bundle,
	wheel runtimewheel.Artifact,
	uvArtifact uvbootstrap.Artifact,
	paths environmentPaths,
	plan runtimeDependencyPlan,
	frozen bool,
) (pythonSelection, error) {
	staged, cleanup, err := a.stageAgentEnvironment(bundle, wheel, uvArtifact, paths, plan)
	if err != nil {
		return pythonSelection{}, err
	}
	defer cleanup()
	if frozen {
		lockPath := filepath.Join(bundle.Directory, runtimeRequirementsLockFile)
		if err := validateFrozenRuntimeLock(bundle, wheel, plan, lockPath); err != nil {
			return pythonSelection{}, err
		}
	}
	if err := a.createAgentVirtualEnvironment(command, bundle, staged); err != nil {
		return pythonSelection{}, err
	}
	lock, cleanupLock, err := a.prepareRuntimeLock(command, bundle, wheel, staged, plan, frozen)
	if err != nil {
		return pythonSelection{}, err
	}
	defer cleanupLock()
	if err := a.syncLockedRuntime(command, staged, lock); err != nil {
		return pythonSelection{}, err
	}
	if err := a.verifyAndRecordFramework(command, bundle, staged, frozen); err != nil {
		return pythonSelection{}, err
	}
	if !frozen {
		if err := refreshRuntimeLockMetadata(bundle, wheel, plan); err != nil {
			return pythonSelection{}, err
		}
	}
	return publishAgentEnvironment(bundle, wheel, paths, staged, plan)
}

type stagedAgentEnvironment struct {
	directory, relative, python, uvPath, wheelPath string
	environment                                    []string
}

func (a *application) stageAgentEnvironment(
	bundle engine.Bundle,
	wheel runtimewheel.Artifact,
	uvArtifact uvbootstrap.Artifact,
	paths environmentPaths,
	plan runtimeDependencyPlan,
) (stagedAgentEnvironment, func(), error) {
	fingerprint, err := environmentFingerprint(bundle, wheel, plan)
	if err != nil {
		return stagedAgentEnvironment{}, nil, err
	}
	relative := environmentRelativePath(fingerprint)
	directory := filepath.Join(paths.root, filepath.FromSlash(relative))
	if info, statErr := os.Lstat(directory); statErr == nil && info.Mode()&os.ModeSymlink != 0 {
		return stagedAgentEnvironment{}, nil, fmt.Errorf("agent environment cannot be a symlink: %s", directory)
	} else if statErr != nil && !os.IsNotExist(statErr) {
		return stagedAgentEnvironment{}, nil, fmt.Errorf("inspect agent environment: %w", statErr)
	}
	uvPath, cleanupUV, err := stageUV(uvArtifact)
	if err != nil {
		return stagedAgentEnvironment{}, nil, err
	}
	wheelPath, cleanupWheel, err := stageRuntimeWheel(wheel)
	if err != nil {
		cleanupUV()
		return stagedAgentEnvironment{}, nil, err
	}
	environment, err := a.agentEnvironmentVariables(bundle, directory)
	if err != nil {
		cleanupWheel()
		cleanupUV()
		return stagedAgentEnvironment{}, nil, err
	}
	cleanup := func() { cleanupWheel(); cleanupUV() }
	return stagedAgentEnvironment{
		directory:   directory,
		relative:    relative,
		python:      runtimePythonPath(directory),
		uvPath:      uvPath,
		wheelPath:   wheelPath,
		environment: environment,
	}, cleanup, nil
}

// createAgentVirtualEnvironment prepares an empty interpreter before the single locked sync.
func (a *application) createAgentVirtualEnvironment(
	command *cobra.Command,
	bundle engine.Bundle,
	staged stagedAgentEnvironment,
) error {
	arguments := []string{
		"venv",
		"--python", bundle.Config.Spec.Runtime.Version,
		"--managed-python",
		// A failed unpublished attempt may leave this content-addressed path behind.
		"--clear",
		staged.directory,
	}
	if err := a.runRuntimeCommandWithEnvironment(
		command, staged.environment, staged.uvPath, arguments...,
	); err != nil {
		return fmt.Errorf("create agent virtual environment: %w", err)
	}
	return nil
}

// syncLockedRuntime makes the hash-verified lock the only installation source.
func (a *application) syncLockedRuntime(
	command *cobra.Command, staged stagedAgentEnvironment, lockPath string,
) error {
	if err := a.runRuntimeCommandWithEnvironment(
		command,
		staged.environment,
		staged.uvPath,
		"pip", "sync", "--python", staged.python, "--require-hashes", lockPath,
	); err != nil {
		return fmt.Errorf("synchronize locked runtime dependencies: %w", err)
	}
	return nil
}

func publishAgentEnvironment(
	bundle engine.Bundle,
	wheel runtimewheel.Artifact,
	paths environmentPaths,
	staged stagedAgentEnvironment,
	plan runtimeDependencyPlan,
) (pythonSelection, error) {
	finalFingerprint, err := environmentFingerprint(bundle, wheel, plan)
	if err != nil {
		return pythonSelection{}, err
	}
	if err := writeEnvironmentState(paths.state, environmentState{
		Fingerprint: finalFingerprint,
		Directory:   staged.relative,
	}); err != nil {
		return pythonSelection{}, err
	}
	return pythonSelection{Executable: staged.python, Source: "agent environment"}, nil
}

func (a *application) agentEnvironmentVariables(
	bundle engine.Bundle, directory string,
) ([]string, error) {
	runtimeDirectory, err := a.runtimeDirectory("")
	if err != nil {
		return nil, err
	}
	overrides := make(map[string]string, len(bundle.Config.Spec.Environment)+3)
	for key, value := range bundle.Config.Spec.Environment {
		overrides[key] = value
	}
	overrides["UV_NO_PROGRESS"] = "1"
	overrides["UV_PROJECT_ENVIRONMENT"] = directory
	overrides["UV_PYTHON_INSTALL_DIR"] = managedPythonDirectory(runtimeDirectory)
	return mergedEnvironment(overrides), nil
}

func writeEnvironmentState(path string, state environmentState) error {
	contents, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return fmt.Errorf("encode agent environment state: %w", err)
	}
	contents = append(contents, '\n')
	temporary := path + ".tmp"
	if err := os.WriteFile(temporary, contents, 0o644); err != nil {
		return fmt.Errorf("write agent environment state: %w", err)
	}
	if err := os.Rename(temporary, path); err != nil {
		_ = os.Remove(temporary)
		return fmt.Errorf("publish agent environment state: %w", err)
	}
	return nil
}

func environmentRelativePath(fingerprint string) string {
	// Virtual environments embed their creation path. Content-addressed final
	// directories allow safe replacement without relocating a prepared venv.
	return filepath.ToSlash(filepath.Join("environments", fingerprint[:16]))
}
