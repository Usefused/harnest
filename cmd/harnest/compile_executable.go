package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/spf13/cobra"
	"harnest.dev/harnest/engine"
	"harnest.dev/harnest/internal/agentlauncher"
	"harnest.dev/harnest/internal/agentpack"
)

type executableOptions struct {
	runtime, format string
	embed           bool
}

// enabled keeps existing directory compilation backward compatible.
func (o executableOptions) enabled() bool {
	return o.runtime != "" || o.embed || o.format == "executable"
}

// validate rejects ambiguous output modes before resolving dependencies or importing code.
func (o executableOptions) validate() error {
	if o.format != "" && o.format != "directory" && o.format != "executable" {
		return fmt.Errorf("--format must be directory or executable")
	}
	if o.format == "directory" && (o.runtime != "" || o.embed) {
		return fmt.Errorf("--runtime and --embed-runtime require executable output")
	}
	return nil
}

// compileExecutable validates the agent against an attached pack without copying dependencies by default.
func (a *application) compileExecutable(command *cobra.Command, source, output, entrypoint string, options executableOptions) error {
	if runtime.GOOS == "windows" && !strings.EqualFold(filepath.Ext(output), ".exe") {
		return fmt.Errorf("Windows executable output must end in .exe")
	}
	bundle, err := loadAgentBundle(source)
	if err != nil {
		return err
	}
	if err = validateAgentBuildOutput(bundle, output); err != nil {
		return err
	}
	if options.runtime == "" {
		return a.compileWithNewPack(command, source, output, entrypoint, options)
	}
	m, root, err := a.prepareExecutableRuntime(bundle, options.runtime)
	if err != nil {
		return err
	}
	temporary, err := os.MkdirTemp("", "harnest-executable-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temporary)
	artifact := filepath.Join(temporary, "agent")
	if err = a.compilePackedArtifact(command, bundle, root, artifact, entrypoint, m); err != nil {
		return err
	}
	if err = a.publishExecutable(command, output, artifact, options, m, bundle.Config.Spec.Environment); err != nil {
		return err
	}
	fmt.Fprintf(command.OutOrStdout(), "Agent executable: %s\nRuntime: %s (embedded: %t)\n", output, m.Digest, options.embed)
	return nil
}

// publishExecutable signs a staged file before atomically replacing the requested executable.
func (a *application) publishExecutable(command *cobra.Command, output, artifact string, options executableOptions, m agentpack.Manifest, environment map[string]string) error {
	launcher, err := loadAgentLauncher(command)
	if err != nil {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(output), 0755); err != nil {
		return err
	}
	stage, err := os.MkdirTemp(filepath.Dir(output), ".agent-publish-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(stage)
	temporary := filepath.Join(stage, "agent")
	if err = agentpack.WriteExecutable(temporary, launcher, artifact, options.runtime, m, options.embed, environment); err != nil {
		return err
	}
	if err = signAgentExecutable(command, a, temporary); err != nil {
		return err
	}
	return os.Rename(temporary, output)
}

// validateAgentBuildOutput keeps executable payloads out of subsequent source snapshots.
func validateAgentBuildOutput(bundle engine.Bundle, output string) error {
	absolute, err := filepath.Abs(output)
	if err != nil {
		return err
	}
	// Paths on different Windows volumes cannot contain one another.
	if !strings.EqualFold(filepath.VolumeName(bundle.Directory), filepath.VolumeName(absolute)) {
		return nil
	}
	relative, err := filepath.Rel(bundle.Directory, absolute)
	if err != nil {
		return err
	}
	if relative == "." {
		return fmt.Errorf("build output cannot replace the agent directory")
	}
	if pathWithinDirectory(bundle.Directory, absolute) && !strings.HasPrefix(filepath.ToSlash(relative), ".harnest/") {
		return fmt.Errorf("output inside an agent must be below .harnest/; use a deployment directory outside the agent source")
	}
	return nil
}

// prepareExecutableRuntime verifies the attachment and shares the launch cache with runtime consumers.
func (a *application) prepareExecutableRuntime(bundle engine.Bundle, pack string) (agentpack.Manifest, string, error) {
	m, err := agentpack.ReadManifest(pack)
	if err != nil {
		return m, "", err
	}
	if err = validatePackAgent(bundle, m); err != nil {
		return m, "", err
	}
	cache, err := agentpack.CacheDirectory(a.system.getenv("HARNEST_AGENT_CACHE"), a.system.userCacheDir)
	if err != nil {
		return m, "", err
	}
	root, err := agentpack.Materialize(pack, cache, m)
	return m, root, err
}

// compilePackedArtifact uses exactly the Python and dependencies that the executable will launch.
func (a *application) compilePackedArtifact(command *cobra.Command, bundle engine.Bundle, root, artifact, entrypoint string, m agentpack.Manifest) error {
	var err error
	if entrypoint == "" {
		entrypoint = bundle.Config.Spec.Entrypoint
	}
	bundle.Config.Spec.Entrypoint = entrypoint
	args := []string{"compile", bundle.Directory, "--output", artifact, "--entrypoint", entrypoint, "--framework", bundle.Config.Spec.Framework.Name, "--mode", bundle.Config.Spec.Framework.EffectiveMode()}
	python, argv := agentpack.PythonCommand(root, m, "harnest.cli", withCLICompilerInterface(args, bundle))
	process := a.system.commandContext(command.Context(), python, argv...)
	process.Env = agentpack.Environment(root, m)
	for key, value := range bundle.Config.Spec.Environment {
		process.Env = append(process.Env, key+"="+value)
	}
	if err = runCommand(process, command.InOrStdin(), io.Discard, command.ErrOrStderr()); err != nil {
		return err
	}
	if _, err = a.system.loadCompiledArtifact(artifact, bundle); err != nil {
		return err
	}
	return nil
}

// compileWithNewPack makes executable-only requests self-contained without changing attachment semantics.
func (a *application) compileWithNewPack(command *cobra.Command, source, output, entrypoint string, options executableOptions) error {
	temporary, err := os.MkdirTemp("", "harnest-pack-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temporary)
	options.runtime = filepath.Join(temporary, "runtime")
	options.embed = true
	store, err := filepath.Abs(filepath.Join(filepath.Dir(output), ".harnest-runtime-objects"))
	if err != nil {
		return err
	}
	if err = a.buildRuntimePack(command, runtimePackOptions{agents: []string{source}, output: options.runtime, store: store}); err != nil {
		return err
	}
	return a.compileExecutable(command, source, output, entrypoint, options)
}

// loadAgentLauncher removes the original macOS signature before adding the agent payload.
func loadAgentLauncher(command *cobra.Command) ([]byte, error) {
	launcher, err := agentlauncher.Load(command.Context())
	if err != nil || !agentpack.HasMachOSignature(launcher) {
		return launcher, err
	}
	file, err := os.CreateTemp("", "harnest-native-*")
	if err != nil {
		return nil, err
	}
	defer os.Remove(file.Name())
	if _, err = file.Write(launcher); err != nil {
		file.Close()
		return nil, err
	}
	if err = file.Close(); err != nil {
		return nil, err
	}
	process := exec.CommandContext(command.Context(), "codesign", "--remove-signature", file.Name())
	if data, err := process.CombinedOutput(); err != nil {
		return nil, fmt.Errorf("prepare launcher signature: %w: %s", err, data)
	}
	return os.ReadFile(file.Name())
}

// signAgentExecutable refreshes Mach-O signatures invalidated by appending the payload.
func signAgentExecutable(command *cobra.Command, a *application, output string) error {
	if runtime.GOOS != "darwin" {
		return nil
	}
	process := a.system.commandContext(command.Context(), "codesign", "--force", "--sign", "-", output)
	process.Stdout, process.Stderr = command.ErrOrStderr(), command.ErrOrStderr()
	if err := process.Run(); err != nil {
		return fmt.Errorf("sign agent executable: %w", err)
	}
	return nil
}

// validatePackAgent requires declared dependency coverage, including extension and MCP requirements.
func validatePackAgent(bundle engine.Bundle, m agentpack.Manifest) error {
	if err := validateAgentDependencyPolicy(bundle); err != nil {
		return err
	}
	plan, err := inspectRuntimeDependencyPlan(bundle)
	if err != nil {
		return err
	}
	identity, err := runtimePackInput(bundle, plan)
	if err != nil {
		return err
	}
	for _, input := range m.Inputs {
		if input == identity {
			return nil
		}
	}
	return fmt.Errorf("runtime does not cover this agent's current dependencies; rebuild it with --agent %s", strings.TrimSpace(bundle.Directory))
}
