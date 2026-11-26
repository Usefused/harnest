// `harnest runtime build`: builds a shared, relocatable Python runtime pack for agent executables.

package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/spf13/cobra"
	"harnest.dev/harnest/internal/agentpack"
)

type runtimePackOptions struct {
	agents               []string
	output, store, wheel string
}

// newRuntimeBuildCommand creates relocatable packs independently from agent executables.
func (a *application) newRuntimeBuildCommand() *cobra.Command {
	var options runtimePackOptions
	command := &cobra.Command{Use: "build --agent AGENT_DIR --output DIRECTORY", Short: "Build a shared, deduplicated Python runtime pack", Args: cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error { return a.buildRuntimePack(command, options) },
	}
	command.Flags().StringArrayVar(&options.agents, "agent", nil, "agent dependency owner (repeat for a shared runtime)")
	command.Flags().StringVarP(&options.output, "output", "o", "", "new runtime pack directory")
	command.Flags().StringVar(&options.store, "store", "", "shared object store (default: .harnest-runtime-objects beside output)")
	command.Flags().StringVar(&options.wheel, "wheel", "", "Harnest wheel override for source builds")
	return command
}

// buildRuntimePack publishes a complete pack, leaving existing destinations untouched.
func (a *application) buildRuntimePack(command *cobra.Command, options runtimePackOptions) error {
	if err := agentpack.SupportedHost(); err != nil {
		return err
	}
	if err := options.validate(); err != nil {
		return err
	}
	output, err := filepath.Abs(options.output)
	if err != nil {
		return err
	}
	if err = requireNewPackPath(output); err != nil {
		return err
	}
	plan, err := sharedRuntimePlanWithPack(options.agents, a.embeddedPack)
	if err != nil {
		return err
	}
	store, err := validatePackDestinations(plan, output, options.store)
	if err != nil {
		return err
	}
	staging, err := os.MkdirTemp(filepath.Dir(output), ".runtime-build-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(staging)
	payload, err := a.buildPackPayload(command, options.wheel, staging, plan)
	if err != nil {
		return err
	}
	manifest, err := publishRuntimePack(output, store, staging, payload, plan)
	if err != nil {
		return err
	}
	fmt.Fprintf(command.OutOrStdout(), "Runtime pack: %s\nDigest: %s\nTarget: %s/%s\n", output, manifest.Digest, manifest.OS, manifest.Arch)
	return nil
}

// validatePackDestinations keeps dependency blobs out of agent snapshots and the published pack's own path.
func validatePackDestinations(plan runtimePackPlan, output, store string) (string, error) {
	if store == "" {
		store = filepath.Join(filepath.Dir(output), ".harnest-runtime-objects")
	}
	store, err := filepath.Abs(store)
	if err != nil {
		return "", err
	}
	if store == output || pathWithinDirectory(output, store) {
		return "", fmt.Errorf("the shared object store must be outside the runtime output directory")
	}
	for _, bundle := range plan.Owners {
		for _, destination := range []string{output, store} {
			if err := validateAgentBuildOutput(bundle, destination); err != nil {
				return "", err
			}
		}
	}
	return store, nil
}

// buildPackPayload owns temporary resolver and wheel assets through dependency installation.
func (a *application) buildPackPayload(command *cobra.Command, override, staging string, plan runtimePackPlan) (packPayload, error) {
	uv, cleanup, err := a.packUV()
	if err != nil {
		return packPayload{}, err
	}
	defer cleanup()
	wheel, cleanupWheel, err := a.packWheel(override)
	if err != nil {
		return packPayload{}, err
	}
	defer cleanupWheel()
	return a.preparePackPayload(command, uv, wheel, staging, plan)
}

// requireNewPackPath prevents rebuilds from mutating packs referenced by existing agents.
func requireNewPackPath(output string) error {
	if _, err := os.Lstat(output); !os.IsNotExist(err) {
		return fmt.Errorf("runtime output must not exist: %s", output)
	}
	return os.MkdirAll(filepath.Dir(output), 0755)
}

// packUV prefers a system-installed uv binary before falling back to the embedded uv artifact.
func (a *application) packUV() (string, func(), error) {
	return a.resolveUV()
}

// packWheel preserves release ownership while allowing explicit contributor wheel builds.
func (a *application) packWheel(override string) (string, func(), error) {
	if override != "" {
		path, err := filepath.Abs(override)
		return path, func() {}, err
	}
	artifact, err := a.system.embeddedWheel(a.version)
	if err != nil {
		return "", nil, fmt.Errorf("load runtime wheel (source builds may pass --wheel): %w", err)
	}
	return stageRuntimeWheel(artifact)
}

// validate rejects incomplete build requests before acquiring tools or creating files.
func (o runtimePackOptions) validate() error {
	if len(o.agents) == 0 || strings.TrimSpace(o.output) == "" {
		return fmt.Errorf("--agent and --output are required")
	}
	return nil
}

type packPayload struct{ Base, Python, Version, Harnest string }

const inspectPortablePython = `import json,pathlib,sys; print(json.dumps({"Base":str(pathlib.Path(sys.base_prefix).resolve()),"Python":str(pathlib.Path(sys.executable).resolve().relative_to(pathlib.Path(sys.base_prefix).resolve())),"Version":sys.version.split()[0]}))`

// preparePackPayload resolves one hash-locked closure and installs into an independent target.
func (a *application) preparePackPayload(command *cobra.Command, uv, wheel, staging string, plan runtimePackPlan) (packPayload, error) {
	payload, python, err := a.packInterpreter(command, uv, staging, plan.Python)
	// Stop immediately if a relocatable Python interpreter cannot be prepared.
	if err != nil {
		return payload, err
	}
	requirements, err := writePackRequirements(staging, wheel, plan)
	// Stop if the combined runtime requirements input cannot be written.
	if err != nil {
		return payload, err
	}
	lock := filepath.Join(staging, "requirements.lock")
	args := []string{"pip", "compile", "--python", python, "--generate-hashes", "--no-header", "--no-annotate", "--output-file", lock, requirements}
	args = append(args, plan.Projects...)
	args = append(args, plan.PackExtensionProjects...)
	constraints, err := packLockConstraints(plan, staging, wheel, a.embeddedPack)
	// Stop if committed lock constraints cannot be prepared for joint resolution.
	if err != nil {
		return payload, err
	}
	for _, constraint := range constraints {
		args = append(args, "--constraint", constraint)
	}
	uvEnv := a.uvEnvironment(nil)
	// Run uv pip compile in staging with shared uv environment handling so corporate indexes work consistently.
	if _, err = a.packCommandOutput(command, staging, uvEnv, uv, args...); err != nil {
		return payload, fmt.Errorf("resolve shared runtime; conflicting agents need separate packs: %w", err)
	}
	packages := filepath.Join(staging, "packages")
	// Run uv pip sync in staging with shared uv environment handling so package installation matches compile.
	if err = a.runRuntimeCommandInDirectory(command, staging, uvEnv, uv, "pip", "sync", "--python", python, "--target", packages, "--link-mode", "copy", "--require-hashes", lock); err != nil {
		return payload, err
	}
	// Stop if console script relocation fails before inspecting the installed Harnest version.
	if err = a.relocatePackTools(command, packages, python); err != nil {
		return payload, err
	}
	result, err := a.packCommandOutput(command, staging, nil, python, "-I", "-c", `import importlib.metadata,json,sys; sys.path.insert(0,sys.argv[1]); print(json.dumps(importlib.metadata.version("harnest")))`, packages)
	// Fail if installed Harnest metadata cannot be read from the staged packages directory.
	if err != nil {
		return payload, err
	}
	// Reject malformed JSON output from the metadata probe.
	if err = json.Unmarshal(result, &payload.Harnest); err != nil {
		return payload, err
	}
	return payload, normalizePackLock(lock, wheel, plan.PackWheelsDir)
}

// packInterpreter selects only uv's relocatable CPython distribution, never a host framework install.
func (a *application) packInterpreter(command *cobra.Command, uv, staging, version string) (packPayload, string, error) {
	var payload packPayload
	uvEnv := a.uvEnvironment(nil)
	// Pin uv python install to staging so a deleted caller CWD cannot abort interpreter download.
	if err := a.runRuntimeCommandInDirectory(command, staging, uvEnv, uv, "python", "install", version, "--no-bin"); err != nil {
		return payload, "", err
	}
	result, err := a.packCommandOutput(command, staging, uvEnv, uv, "python", "find", "--managed-python", "--system", "--no-project", version)
	// Stop if uv cannot locate the installed relocatable interpreter.
	if err != nil {
		return payload, "", err
	}
	python := strings.TrimSpace(string(result))
	result, err = a.packCommandOutput(command, staging, nil, python, "-I", "-c", inspectPortablePython)
	// Stop if the discovered Python binary cannot report its base prefix layout.
	if err != nil {
		return payload, "", err
	}
	err = json.Unmarshal(result, &payload)
	return payload, python, err
}

// packCommandOutput keeps machine-readable interpreter discovery separate from diagnostics.
func (a *application) packCommandOutput(command *cobra.Command, directory string, environment []string, executable string, args ...string) ([]byte, error) {
	var output bytes.Buffer
	process := a.system.commandContext(command.Context(), executable, args...)
	// Pin process.Dir to an existing staging directory so uv and Python never fail on an unlinked caller CWD.
	if directory != "" {
		process.Dir = directory
	}
	// Apply explicit subprocess environment variables when provided for uv invocations.
	if len(environment) != 0 {
		process.Env = environment
	}
	process.Stderr = command.ErrOrStderr()
	process.Stdout = &output
	err := process.Run()
	return output.Bytes(), err
}

// normalizePackLock removes machine-local release and pack wheel paths from the retained build record.
func normalizePackLock(filename, wheel, packWheelsDir string) error {
	data, err := os.ReadFile(filename)
	if err != nil {
		return err
	}
	value := strings.ReplaceAll(string(data), runtimeWheelURI(wheel), runtimeWheelMarker)
	if packWheelsDir != "" {
		packPrefix := strings.TrimSuffix(runtimeWheelURI(packWheelsDir), "/") + "/"
		value = strings.ReplaceAll(value, packPrefix, packWheelsLockMarker+"/")
	}
	return os.WriteFile(filename, []byte(value), 0600)
}

// relocatePackScripts removes interpreter paths that would break after moving a pack.
func relocatePackScripts(packages, python string) error {
	directory := filepath.Join(packages, "bin")
	entries, err := os.ReadDir(directory)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		filename := filepath.Join(directory, entry.Name())
		data, err := os.ReadFile(filename)
		if err != nil {
			return err
		}
		relocated := portableConsoleScript(data, python)
		if !bytes.Equal(data, relocated) {
			if err = os.WriteFile(filename, relocated, 0755); err != nil {
				return err
			}
		}
	}
	return nil
}

// publishRuntimePack gives every portable pack its own links into a shared content store.
func publishRuntimePack(output, store, staging string, payload packPayload, plan runtimePackPlan) (agentpack.Manifest, error) {
	m := agentpack.Manifest{Format: agentpack.Format, OS: runtime.GOOS, Arch: runtime.GOARCH, Python: filepath.ToSlash(filepath.Join("python", payload.Python)), Version: payload.Version, Harnest: payload.Harnest, Inputs: plan.Inputs}
	for _, tree := range []struct{ root, prefix string }{{payload.Base, "python"}, {filepath.Join(staging, "packages"), "packages"}} {
		files, err := agentpack.ScanTree(tree.root, tree.prefix, store)
		if err != nil {
			return m, err
		}
		m.Files = append(m.Files, files...)
	}
	lock, err := agentpack.StoreFile(filepath.Join(staging, "requirements.lock"), store)
	if err != nil {
		return m, err
	}
	lock.Path = "requirements.lock"
	m.Files = append(m.Files, lock)
	if err = storeRuntimeBuildReport(staging, store, plan, &m); err != nil {
		return m, err
	}
	m.Seal()
	pack := filepath.Join(staging, "pack")
	if err = os.MkdirAll(pack, 0700); err != nil {
		return m, err
	}
	if err = linkPackObjects(store, pack, m.Files); err != nil {
		return m, err
	}
	if err = agentpack.WriteManifest(pack, m); err != nil {
		return m, err
	}
	if err = copyRuntimeBuildReport(staging, pack); err != nil {
		return m, err
	}
	return m, os.Rename(pack, output)
}

// portableConsoleScript handles uv's direct and shell-quoted Python shebangs without rewriting ordinary shell tools.
func portableConsoleScript(data []byte, python string) []byte {
	prefix := []byte("#!" + python + "\n")
	if bytes.HasPrefix(data, prefix) {
		return append([]byte("#!/usr/bin/env python3\n"), data[len(prefix):]...)
	}
	lines := bytes.SplitN(data, []byte("\n"), 4)
	if len(lines) != 4 {
		return data
	}
	// uv uses this polyglot header when the build interpreter path contains spaces.
	if string(lines[0]) == "#!/bin/sh" && bytes.HasPrefix(lines[1], []byte("'''exec' ")) && string(lines[2]) == "' '''" {
		return append([]byte("#!/usr/bin/env python3\n"), lines[3]...)
	}
	return data
}
