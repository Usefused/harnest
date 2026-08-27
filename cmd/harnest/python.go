package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"harnest.dev/harnest/engine"
)

type pythonSelection struct {
	Executable string
	Source     string
}

func (a *application) resolvePython() (pythonSelection, error) {
	if value := strings.TrimSpace(a.pythonFlag); value != "" {
		return a.resolvePythonCandidate(value, "--python")
	}
	if value := strings.TrimSpace(a.system.getenv("HARNEST_PYTHON")); value != "" {
		return a.resolvePythonCandidate(value, "HARNEST_PYTHON")
	}

	runtimeDirectory, _ := a.runtimeDirectory("")
	if runtimeDirectory != "" {
		managed := filepath.Join(runtimeDirectory, "bin", "python")
		if executable, err := a.system.lookPath(managed); err == nil {
			return pythonSelection{Executable: executable, Source: "managed runtime"}, nil
		}
	}
	if executable, err := a.system.lookPath("python3"); err == nil {
		return pythonSelection{Executable: executable, Source: "PATH"}, nil
	}
	return pythonSelection{}, fmt.Errorf(
		"Python runtime not found; install Harnest, set HARNEST_PYTHON, or pass --python",
	)
}

func (a *application) runtimeDirectory(requested string) (string, error) {
	if value := strings.TrimSpace(requested); value != "" {
		return value, nil
	}
	if value := strings.TrimSpace(a.system.getenv("HARNEST_RUNTIME_DIR")); value != "" {
		return value, nil
	}
	home, err := a.system.userHomeDir()
	if err != nil {
		return "", fmt.Errorf("resolve home directory for managed runtime: %w", err)
	}
	return filepath.Join(home, ".harnest", "runtime"), nil
}

func (a *application) resolvePythonCandidate(value, source string) (pythonSelection, error) {
	executable, err := a.system.lookPath(value)
	if err != nil {
		return pythonSelection{}, fmt.Errorf("%s Python executable %q is unavailable: %w", source, value, err)
	}
	return pythonSelection{Executable: executable, Source: source}, nil
}

func configuredEnvironment(bundle engine.Bundle) []string {
	overrides := make(map[string]string, len(bundle.Config.Spec.Environment)+1)
	for key, value := range bundle.Config.Spec.Environment {
		overrides[key] = value
	}
	overrides["PYTHONDONTWRITEBYTECODE"] = "1"
	return mergedEnvironment(overrides)
}

func mergedEnvironment(overrides map[string]string) []string {
	values := make(map[string]string)
	for _, item := range os.Environ() {
		key, value, found := strings.Cut(item, "=")
		if found {
			values[key] = value
		}
	}
	for key, value := range overrides {
		values[key] = value
	}
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	environment := make([]string, 0, len(keys))
	for _, key := range keys {
		environment = append(environment, key+"="+values[key])
	}
	return environment
}

func loadAgentBundle(value string) (engine.Bundle, error) {
	directory := value
	info, err := os.Stat(value)
	if err == nil && !info.IsDir() {
		directory = filepath.Dir(value)
	}
	bundle, err := engine.LoadBundle(directory)
	if err != nil {
		return engine.Bundle{}, fmt.Errorf("load agent bundle: %w", err)
	}
	return bundle, nil
}

func runPythonCLI(
	ctx context.Context,
	app *application,
	python pythonSelection,
	arguments []string,
	environment []string,
	stdin io.Reader,
	stdout, stderr io.Writer,
) error {
	commandArguments := append([]string{"-m", "harnest.cli"}, arguments...)
	command := app.system.commandContext(ctx, python.Executable, commandArguments...)
	if environment != nil {
		command.Env = environment
	}
	if err := runCommand(command, stdin, stdout, stderr); err != nil {
		return fmt.Errorf("run Python Harnest CLI: %w", err)
	}
	return nil
}
