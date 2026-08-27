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

	runtimeDirectory := strings.TrimSpace(a.system.getenv("HARNEST_RUNTIME_DIR"))
	if runtimeDirectory == "" {
		home, err := a.system.userHomeDir()
		if err == nil {
			runtimeDirectory = filepath.Join(home, ".harnest", "runtime")
		}
	}
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

func (a *application) resolvePythonCandidate(value, source string) (pythonSelection, error) {
	executable, err := a.system.lookPath(value)
	if err != nil {
		return pythonSelection{}, fmt.Errorf("%s Python executable %q is unavailable: %w", source, value, err)
	}
	return pythonSelection{Executable: executable, Source: source}, nil
}

func configuredEnvironment(bundle engine.Bundle) []string {
	values := make(map[string]string)
	for _, item := range os.Environ() {
		key, value, found := strings.Cut(item, "=")
		if found {
			values[key] = value
		}
	}
	for key, value := range bundle.Config.Spec.Environment {
		values[key] = value
	}
	values["PYTHONDONTWRITEBYTECODE"] = "1"
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
