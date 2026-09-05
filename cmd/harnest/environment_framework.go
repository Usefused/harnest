package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"
	"harnest.dev/harnest/engine"
)

var lockedFrameworkVersion = regexp.MustCompile(`^[0-9][A-Za-z0-9.!+_-]*$`)

type projectFrameworkPin struct {
	Name         string `yaml:"name"`
	Distribution string `yaml:"distribution"`
	Version      string `yaml:"version"`
}

// lockedFrameworkRequirement constrains environment creation before any framework is installed.
func lockedFrameworkRequirement(bundle engine.Bundle) (string, error) {
	path := filepath.Join(bundle.Directory, "harnest.lock")
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() || info.Size() > 65536 {
		return "", fmt.Errorf("harnest.lock must be a regular file of at most 64KiB")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return parseFrameworkRequirement(data, bundle.Config.Spec.Framework.Name)
}

// parseFrameworkRequirement permits an intentional framework switch but never an arbitrary package.
func parseFrameworkRequirement(data []byte, framework string) (string, error) {
	var lock struct {
		Framework *projectFrameworkPin `yaml:"framework"`
	}
	if err := yaml.Unmarshal(data, &lock); err != nil {
		return "", fmt.Errorf("parse harnest.lock: %w", err)
	}
	pin := lock.Framework
	if pin == nil {
		return "", nil
	}
	distributions := map[string]string{"adk": "google-adk", "langgraph": "langgraph"}
	distribution, known := distributions[pin.Name]
	if !known || pin.Distribution != distribution || !lockedFrameworkVersion.MatchString(pin.Version) {
		return "", fmt.Errorf("invalid resolved framework in harnest.lock")
	}
	if pin.Name != framework {
		return "", nil
	}
	return pin.Distribution + "==" + pin.Version, nil
}

// recordFrameworkResolution reads metadata from the actual installed project interpreter.
func (a *application) recordFrameworkResolution(command *cobra.Command, bundle engine.Bundle, staged stagedAgentEnvironment, frozen bool) error {
	args := []string{"-m", "harnest.project_lock", bundle.Directory, bundle.Config.Spec.Framework.Name}
	if frozen {
		args = append(args, "--frozen")
	}
	if err := a.runRuntimeCommandWithEnvironment(command, staged.environment, staged.python, args...); err != nil {
		return fmt.Errorf("record resolved framework in harnest.lock: %w", err)
	}
	return nil
}

// verifyAndRecordFramework refuses an environment with incompatible transitive dependencies.
func (a *application) verifyAndRecordFramework(command *cobra.Command, bundle engine.Bundle, staged stagedAgentEnvironment, frozen bool) error {
	if err := a.runRuntimeCommandWithEnvironment(command, staged.environment, staged.uvPath, "pip", "check", "--python", staged.python); err != nil {
		return fmt.Errorf("verify resolved dependency compatibility: %w", err)
	}
	return a.recordFrameworkResolution(command, bundle, staged, frozen)
}
