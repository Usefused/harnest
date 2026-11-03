// Command hostlier-harnest packages a Harnest agent for an ordinary Hostlier container workload.
package main

import (
	"context"
	"debug/elf"
	"encoding/json"
	"errors"
	"fmt"
	"gopkg.in/yaml.v3"
	"harnest.dev/harnest/engine"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"regexp"
	"runtime"
	"strconv"

	"harnest.dev/harnest/internal/agentpack"
)

// Only immutable image references may enter the generated Dockerfile.
var pinnedImage = regexp.MustCompile(`^[a-z0-9][a-zA-Z0-9._/:-]*@sha256:[0-9a-f]{64}$`)

type buildSettings struct {
	project, artifact, base, architecture, output string
	port                                          int
}

// validate checks target inputs before allocating output or invoking the compiler.
func (s buildSettings) validate() error {
	if s.architecture != "amd64" && s.architecture != "arm64" {
		return errors.New("select a Linux amd64 or arm64 target")
	}
	if !pinnedImage.MatchString(s.base) {
		return errors.New("base image must be pinned by sha256 digest")
	}
	if s.port < 1 || s.port > 65535 || s.output == "" {
		return errors.New("specify a new output directory and a valid serving port")
	}
	return nil
}

// prepare stages only the compiled executable and its recipe, never the developer's source tree.
func prepare(ctx context.Context, s buildSettings) error {
	if err := s.validate(); err != nil {
		return err
	}
	if _, err := os.Lstat(s.output); !os.IsNotExist(err) {
		return errors.New("output directory must be new")
	}
	parent := filepath.Dir(s.output)
	if err := os.MkdirAll(parent, 0700); err != nil {
		return err
	}
	stage, err := os.MkdirTemp(parent, ".hostlier-harnest-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(stage)
	agent := filepath.Join(stage, "agent")
	if err := stageExecutable(ctx, s, agent); err != nil {
		return err
	}
	if err := verifyAgent(agent, s.architecture, nil); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(stage, "Dockerfile"), []byte(recipe(s)), 0600); err != nil {
		return err
	}
	// A completed nonempty context cannot be overwritten by a concurrent prepare.
	return os.Rename(stage, s.output)
}

// stageExecutable builds using the target host's CLI or accepts a Linux CI artifact for cross-platform packaging.
func stageExecutable(ctx context.Context, s buildSettings, dest string) error {
	if s.artifact != "" {
		return copyExecutable(s.artifact, dest)
	}
	if runtime.GOOS != "linux" || runtime.GOARCH != s.architecture {
		return errors.New("compile on the target Linux platform, or set packages.harnest.artifact in .hostlier to an embedded Linux build")
	}
	// The compiler can emit sibling reports. Move only its executable into the container context.
	work, err := os.MkdirTemp(filepath.Dir(filepath.Dir(dest)), ".harnest-compile-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(work)
	binary := filepath.Join(work, "agent")
	command := exec.CommandContext(ctx, "harnest", "compile", s.project, "--format", "executable", "--output", binary)
	command.Stdout, command.Stderr = os.Stderr, os.Stderr
	if err := command.Run(); err != nil {
		return fmt.Errorf("compiling Harnest agent: %w", err)
	}
	return os.Rename(binary, dest)
}

// copyExecutable snapshots the input before checking metadata so validation covers the published bytes.
func copyExecutable(source, destination string) error {
	info, err := os.Stat(source)
	if err != nil || !info.Mode().IsRegular() {
		return errors.New("agent artifact must be a regular file")
	}
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	output, err := os.OpenFile(destination, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0755)
	if err != nil {
		return err
	}
	_, err = io.Copy(output, input)
	return errors.Join(err, output.Close())
}

// verifyAgent checks the actual ELF target and embedded Harnest runtime without executing supplied code.
func verifyAgent(path, arch string, expected *engine.AgentConfig) error {
	if err := verifyELF(path, arch); err != nil {
		return err
	}
	return verifyRuntime(path, arch, expected)
}

// verifyELF checks native machine metadata independently of the appended Harnest manifest.
func verifyELF(path, arch string) error {
	native, err := elf.Open(path)
	if err != nil {
		return errors.New("expected a compiled Linux ELF executable")
	}
	defer native.Close()
	machines := map[string]elf.Machine{"amd64": elf.EM_X86_64, "arm64": elf.EM_AARCH64}
	if native.Machine != machines[arch] || native.Class != elf.ELFCLASS64 {
		return errors.New("agent ELF architecture differs from target")
	}
	return nil
}

// verifyRuntime binds the embedded dependency inventory to the executable reference.
func verifyRuntime(path, arch string, expected *engine.AgentConfig) error {
	archive, metadata, err := agentpack.ReadExecutable(path)
	if err != nil {
		return fmt.Errorf("invalid compiled Harnest agent: %w", err)
	}
	defer archive.Close()
	if !metadata.Embedded || metadata.Runtime.OS != "linux" || metadata.Runtime.Arch != arch {
		return errors.New("expected a target-matching embedded Harnest runtime")
	}
	if expected != nil {
		if err := verifyCompiledConfig(archive, expected); err != nil {
			return err
		}
	}
	return verifyInventory(archive, metadata)
}

// verifyCompiledConfig prevents reviewing one agent's settings while deploying a binary built for another.
func verifyCompiledConfig(archive *agentpack.Archive, expected *engine.AgentConfig) error {
	file, err := archive.Open("agent/source/config.yaml")
	if err != nil {
		return errors.New("compiled agent config is missing")
	}
	defer file.Close()
	var config engine.AgentConfig
	if err := yaml.NewDecoder(io.LimitReader(file, 4<<20)).Decode(&config); err != nil || !reflect.DeepEqual(config, *expected) {
		return errors.New("compiled agent config differs from the project; rebuild the selected artifact")
	}
	return nil
}

// verifyInventory confirms that the embedded pack is the platform and digest recorded by the launcher.
func verifyInventory(archive *agentpack.Archive, metadata agentpack.Executable) error {
	file, err := archive.Open("runtime/runtime-manifest.json")
	if err != nil {
		return errors.New("embedded runtime manifest is missing")
	}
	defer file.Close()
	var pack agentpack.Manifest
	if err := json.NewDecoder(io.LimitReader(file, 16<<20)).Decode(&pack); err != nil {
		return errors.New("invalid embedded runtime manifest")
	}
	if pack.Digest != metadata.Runtime.Digest || pack.Arch != metadata.Runtime.Arch || pack.OS != "linux" {
		return errors.New("embedded runtime identity mismatch")
	}
	return pack.Validate()
}

// recipe uses the compiled launcher's public serve interface and a writable runtime materialization cache.
func recipe(s buildSettings) string {
	return "FROM " + s.base + "\nWORKDIR /opt/agent\nCOPY --chmod=0555 agent ./agent\nENV HARNEST_AGENT_CACHE=/tmp/harnest-cache\nENTRYPOINT [\"/opt/agent/agent\"]\nCMD [\"serve\",\"--allow-remote\",\"--host\",\"0.0.0.0\",\"--port\",\"" + strconv.Itoa(s.port) + "\"]\n"
}
