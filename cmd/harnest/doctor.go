package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
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
    ("google.adk", "google-adk"),
    ("litellm", "litellm"),
    ("pytest", "pytest"),
    ("opentelemetry.sdk", "opentelemetry-sdk"),
    ("opentelemetry.exporter.otlp.proto.http.trace_exporter", "opentelemetry-exporter-otlp-proto-http"),
    ("opentelemetry.instrumentation.fastapi", "opentelemetry-instrumentation-fastapi"),
    ("opentelemetry.instrumentation.logging", "opentelemetry-instrumentation-logging"),
]
if framework == "langgraph":
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

func (a *application) newDoctorCommand() *cobra.Command {
	var framework string
	command := &cobra.Command{
		Use:   "doctor",
		Short: "Diagnose the Go CLI, Python runtime, and required packages",
		Args:  cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			return a.runDoctor(command, framework)
		},
	}
	command.Flags().StringVar(&framework, "framework", "adk", "framework dependencies to diagnose: adk or langgraph")
	return command
}

func (a *application) runDoctor(command *cobra.Command, framework string) error {
	if framework != "adk" && framework != "langgraph" {
		return fmt.Errorf("--framework must be adk or langgraph")
	}
	writer := command.OutOrStdout()
	fmt.Fprintf(writer, "[ok] Go CLI: harnest %s\n", a.version)
	python, err := a.resolvePython()
	if err != nil {
		fmt.Fprintf(writer, "[fail] Python runtime: %v\n", err)
		return fmt.Errorf("doctor found a Python runtime problem")
	}
	result, err := a.probePython(command, python, framework)
	if err != nil {
		return err
	}
	problems := writeDoctorResult(writer, python, result)
	if problems != 0 {
		return fmt.Errorf("doctor found %d problem(s)", problems)
	}
	fmt.Fprintln(writer, "Harnest is ready.")
	return nil
}

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
