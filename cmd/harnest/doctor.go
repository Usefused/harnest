package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"
)

const doctorProbe = `import importlib
import importlib.metadata
import json
import platform
import sys

framework = sys.argv[1]
dependencies = [
    ("harnest", "harnest"),
    ("litellm", "litellm"),
    ("opentelemetry.sdk", "opentelemetry-sdk"),
    ("opentelemetry.exporter.otlp.proto.http.trace_exporter", "opentelemetry-exporter-otlp-proto-http"),
    ("opentelemetry.instrumentation.fastapi", "opentelemetry-instrumentation-fastapi"),
    ("opentelemetry.instrumentation.logging", "opentelemetry-instrumentation-logging"),
]
if framework == "adk":
    dependencies.append(("google.adk", "google-adk"))
elif framework == "langgraph":
    dependencies.extend((
        ("langgraph", "langgraph"),
        ("langchain", "langchain"),
        ("langchain_litellm", "langchain-litellm"),
    ))

checks = []
for module, distribution in dependencies:
    try:
        importlib.import_module(module)
        checks.append({
            "name": distribution,
            "ok": True,
            "version": importlib.metadata.version(distribution),
            "error": "",
        })
    except Exception as exc:
        checks.append({
            "name": distribution,
            "ok": False,
            "version": "",
            "error": f"{type(exc).__name__}: {exc}",
        })

print(json.dumps({
    "executable": sys.executable,
    "python": platform.python_version(),
    "supported": sys.version_info >= (3, 10),
    "packages": checks,
}, separators=(",", ":")))
`

type doctorResult struct {
	Executable string          `json:"executable"`
	Python     string          `json:"python"`
	Supported  bool            `json:"supported"`
	Packages   []doctorPackage `json:"packages"`
}

type doctorPackage struct {
	Name    string `json:"name"`
	OK      bool   `json:"ok"`
	Version string `json:"version"`
	Error   string `json:"error"`
}

// newDoctorCommand inspects core packages or an agent's existing environment.
func (a *application) newDoctorCommand() *cobra.Command {
	var framework string
	command := &cobra.Command{
		Use:   "doctor [AGENT_DIR]",
		Short: "Diagnose the Go CLI, Python runtime, and required packages",
		Args:  cobra.MaximumNArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			return a.runDoctor(command, arguments, framework)
		},
	}
	command.Flags().StringVar(&framework, "framework", "", "framework dependencies: adk or langgraph (default: agent framework, otherwise core only)")
	return command
}

// runDoctor reports the selected environment without installing dependencies.
func (a *application) runDoctor(command *cobra.Command, arguments []string, framework string) error {
	if framework != "" && framework != "adk" && framework != "langgraph" {
		return fmt.Errorf("--framework must be adk or langgraph")
	}
	writer := command.OutOrStdout()
	fmt.Fprintf(writer, "[ok] Go CLI: harnest %s\n", a.version)
	python, framework, err := a.doctorPython(arguments, framework)
	if err != nil {
		fmt.Fprintf(writer, "[fail] Python runtime: %v\n", err)
		return fmt.Errorf("doctor found a Python runtime problem")
	}
	if framework == "" {
		fmt.Fprintln(writer, "Checking core runtime only; use doctor AGENT_DIR to check an agent.")
	} else {
		fmt.Fprintf(writer, "Checking %s framework dependencies.\n", framework)
	}
	result, err := a.probePython(command, python, framework)
	if err != nil {
		return err
	}
	problems := writeDoctorResult(writer, python, result)
	if problems != 0 {
		fmt.Fprintln(writer, "For an agent, run harnest env sync AGENT_DIR, then harnest doctor AGENT_DIR.")
		return fmt.Errorf("doctor found %d problem(s)", problems)
	}
	fmt.Fprintln(writer, "Harnest is ready.")
	return nil
}

// doctorPython detects the project while preserving explicit interpreter choices.
func (a *application) doctorPython(arguments []string, framework string) (pythonSelection, string, error) {
	directory, err := doctorProjectDirectory(arguments)
	if err != nil {
		return pythonSelection{}, framework, err
	}
	if directory != "" {
		bundle, err := loadAgentBundle(directory)
		if err != nil {
			return pythonSelection{}, framework, err
		}
		if framework == "" {
			framework = bundle.Config.Spec.Framework.Name
		}
	}
	if directory == "" || strings.TrimSpace(a.pythonFlag) != "" || strings.TrimSpace(a.system.getenv("HARNEST_PYTHON")) != "" {
		python, err := a.resolvePython()
		return python, framework, err
	}
	python, err := existingDoctorEnvironment(directory)
	return python, framework, err
}

// doctorProjectDirectory treats an explicit target as mandatory and auto-detects cwd.
func doctorProjectDirectory(arguments []string) (string, error) {
	if len(arguments) != 0 {
		return filepath.Abs(arguments[0])
	}
	directory, err := os.Getwd()
	if err != nil {
		return "", err
	}
	if _, err := os.Stat(filepath.Join(directory, "config.yaml")); os.IsNotExist(err) {
		return "", nil
	} else if err != nil {
		return "", err
	}
	return directory, nil
}

// existingDoctorEnvironment reads the published runtime without syncing or pruning it.
func existingDoctorEnvironment(directory string) (pythonSelection, error) {
	root := filepath.Join(directory, ".harnest")
	// Serve and eval may have synced their own profile before runtime was used.
	for _, profile := range environmentProfiles {
		paths := environmentPaths{root: root, state: filepath.Join(root, profile.stateFile())}
		if python, found := publishedDoctorEnvironment(paths); found {
			python.Source = fmt.Sprintf("agent %s environment", profile)
			return python, nil
		}
	}
	return pythonSelection{}, fmt.Errorf("agent environment unavailable; run harnest env sync %q, then retry doctor", directory)
}

// publishedDoctorEnvironment validates a saved pointer before probing its interpreter.
func publishedDoctorEnvironment(paths environmentPaths) (pythonSelection, bool) {
	contents, err := readRegularDependencyFile(paths.state)
	var state environmentState
	if err == nil && json.Unmarshal(contents, &state) == nil && state.Fingerprint != "" {
		return cachedAgentPython(paths, state.Fingerprint)
	}
	return pythonSelection{}, false
}

// probePython imports packages in the interpreter selected for this diagnostic.
func (a *application) probePython(command *cobra.Command, python pythonSelection, framework string) (doctorResult, error) {
	probe := a.system.commandContext(command.Context(), python.Executable, "-c", doctorProbe, framework)
	var stdout, stderr bytes.Buffer
	probe.Stdout, probe.Stderr = &stdout, &stderr
	if err := probe.Run(); err != nil {
		detail := strings.TrimSpace(stderr.String())
		if detail == "" {
			detail = err.Error()
		}
		fmt.Fprintf(command.OutOrStdout(), "[fail] Python runtime (%s): %s\n", python.Executable, detail)
		return doctorResult{}, fmt.Errorf("doctor could not inspect the Python runtime")
	}
	var result doctorResult
	decoder := json.NewDecoder(&stdout)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&result); err != nil {
		return doctorResult{}, fmt.Errorf("decode Python doctor response: %w", err)
	}
	return result, nil
}

func writeDoctorResult(writer io.Writer, python pythonSelection, result doctorResult) int {
	problems := 0
	if result.Supported {
		fmt.Fprintf(writer, "[ok] Python: %s (%s, selected from %s)\n", result.Python, result.Executable, python.Source)
	} else {
		problems++
		fmt.Fprintf(writer, "[fail] Python: %s; Harnest requires Python 3.10 or newer\n", result.Python)
	}
	for _, dependency := range result.Packages {
		if dependency.OK {
			fmt.Fprintf(writer, "[ok] Python package: %s %s\n", dependency.Name, dependency.Version)
		} else {
			problems++
			fmt.Fprintf(writer, "[fail] Python package: %s (%s)\n", dependency.Name, dependency.Error)
		}
	}
	return problems
}
