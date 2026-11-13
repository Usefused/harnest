// Chooses the Python interpreter for a command and loads the agent bundle it runs against.

package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"

	"harnest.dev/harnest/engine"
)

type pythonSelection struct {
	Executable string
	Source     string
	release    func()
}

// releaseLease relinquishes managed-environment ownership exactly once.
func (p pythonSelection) releaseLease() {
	if p.release != nil {
		p.release()
	}
}

// resolvePython prefers explicit overrides, then the managed runtime and host PATH.
func (a *application) resolvePython() (pythonSelection, error) {
	if value := strings.TrimSpace(a.pythonFlag); value != "" {
		return a.resolvePythonCandidate(value, "--python")
	}
	if value := strings.TrimSpace(a.system.getenv("HARNEST_PYTHON")); value != "" {
		return a.resolvePythonCandidate(value, "HARNEST_PYTHON")
	}

	runtimeDirectory, _ := a.runtimeDirectory("")
	if runtimeDirectory != "" {
		// The managed runtime is a uv environment, so its interpreter lives
		// under Scripts\python.exe on Windows rather than bin/python.
		managed := runtimePythonPath(runtimeDirectory)
		if executable, err := a.system.lookPath(managed); err == nil {
			return pythonSelection{Executable: executable, Source: "managed runtime"}, nil
		}
	}
	for _, name := range pathPythonCandidates(runtime.GOOS) {
		if executable, err := a.system.lookPath(name); err == nil {
			return pythonSelection{Executable: executable, Source: "PATH"}, nil
		}
	}
	return pythonSelection{}, fmt.Errorf(
		"Python runtime not found; install Harnest, set HARNEST_PYTHON, or pass --python",
	)
}

// pathPythonCandidates lists PATH names in preference order for one host OS.
func pathPythonCandidates(goos string) []string {
	if goos == "windows" {
		// Windows installers register python.exe; python3.exe is usually absent
		// or the Microsoft Store placeholder, so it is only a last resort.
		return []string{"python", "python3"}
	}
	return []string{"python3"}
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

// runPythonCLI preserves command failures and streams test output without buffering.
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
	// Progress must reach the parent while an eval is waiting on model calls,
	// even when the spinner wraps stdout/stderr in subprocess pipes.
	if len(arguments) > 0 && arguments[0] == "test" {
		commandArguments = append([]string{"-u"}, commandArguments...)
	}
	command := app.system.commandContext(ctx, python.Executable, commandArguments...)
	if environment != nil {
		command.Env = environment
	}
	if err := runCommand(command, stdin, stdout, stderr); err != nil {
		return fmt.Errorf("run Python Harnest CLI: %w", err)
	}
	return nil
}
