package main

import (
	"bytes"
	"encoding/json"
	"fmt"
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
    ("pytest", "pytest"),
    ("opentelemetry.sdk", "opentelemetry-sdk"),
    ("opentelemetry.exporter.otlp.proto.http.trace_exporter", "opentelemetry-exporter-otlp-proto-http"),
    ("opentelemetry.instrumentation.fastapi", "opentelemetry-instrumentation-fastapi"),
    ("opentelemetry.instrumentation.logging", "opentelemetry-instrumentation-logging"),
]
if framework == "adk":
    dependencies.append(("google.adk", "google-adk"))
else:
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

			probe := a.system.commandContext(command.Context(), python.Executable, "-c", doctorProbe, framework)
			var stdout bytes.Buffer
			var stderr bytes.Buffer
			probe.Stdout = &stdout
			probe.Stderr = &stderr
			if err := probe.Run(); err != nil {
				detail := strings.TrimSpace(stderr.String())
				if detail == "" {
					detail = err.Error()
				}
				fmt.Fprintf(writer, "[fail] Python runtime (%s): %s\n", python.Executable, detail)
				return fmt.Errorf("doctor could not inspect the Python runtime")
			}

			var result doctorResult
			decoder := json.NewDecoder(&stdout)
			decoder.DisallowUnknownFields()
			if err := decoder.Decode(&result); err != nil {
				return fmt.Errorf("decode Python doctor response: %w", err)
			}
			problems := 0
			if result.Supported {
				fmt.Fprintf(
					writer,
					"[ok] Python: %s (%s, selected from %s)\n",
					result.Python,
					result.Executable,
					python.Source,
				)
			} else {
				problems++
				fmt.Fprintf(writer, "[fail] Python: %s; Harnest requires Python 3.10 or newer\n", result.Python)
			}
			for _, dependency := range result.Packages {
				if dependency.OK {
					fmt.Fprintf(writer, "[ok] Python package: %s %s\n", dependency.Name, dependency.Version)
					continue
				}
				problems++
				fmt.Fprintf(writer, "[fail] Python package: %s (%s)\n", dependency.Name, dependency.Error)
			}
			if problems != 0 {
				return fmt.Errorf("doctor found %d problem(s)", problems)
			}
			fmt.Fprintln(writer, "Harnest is ready.")
			return nil
		},
	}
	command.Flags().StringVar(&framework, "framework", "adk", "framework dependencies to diagnose: adk or langgraph")
	return command
}
