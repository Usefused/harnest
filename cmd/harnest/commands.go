package main

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/engine"
)

func (a *application) newCompileCommand() *cobra.Command {
	var output string
	var entrypoint string
	command := &cobra.Command{
		Use:   "compile AGENT_DIR --output DIRECTORY",
		Short: "Compile and validate an agent as a standalone artifact",
		Args:  cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			if strings.TrimSpace(output) == "" {
				return fmt.Errorf("--output is required")
			}
			bundle, err := loadAgentBundle(arguments[0])
			if err != nil {
				return err
			}
			selectedEntrypoint := entrypoint
			if selectedEntrypoint == "" {
				selectedEntrypoint = bundle.Config.Spec.Entrypoint
			}
			python, err := a.resolvePython()
			if err != nil {
				return err
			}
			return runPythonCLI(
				command.Context(),
				a,
				python,
				[]string{
					"compile", bundle.Directory, "--output", output,
					"--entrypoint", selectedEntrypoint,
					"--framework", bundle.Config.Spec.Framework.Name,
					"--mode", bundle.Config.Spec.Framework.EffectiveMode(),
				},
				configuredEnvironment(bundle),
				command.InOrStdin(),
				command.OutOrStdout(),
				command.ErrOrStderr(),
			)
		},
	}
	command.Flags().StringVarP(&output, "output", "o", "", "compiled artifact directory")
	command.Flags().StringVar(
		&entrypoint,
		"entrypoint",
		"",
		"source module:symbol (default: spec.entrypoint from config.yaml)",
	)
	return command
}

func (a *application) newTestCommand() *cobra.Command {
	var includeSmoke bool
	var includeEvals bool
	var evalTrajectory string
	command := &cobra.Command{
		Use:   "test AGENT_DIR",
		Short: "Compile an agent and run its authored tests",
		Args:  cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			if evalTrajectory != "business" && evalTrajectory != "strict" {
				return fmt.Errorf("--eval-trajectory must be business or strict")
			}
			bundle, err := loadAgentBundle(arguments[0])
			if err != nil {
				return err
			}
			python, err := a.resolvePython()
			if err != nil {
				return err
			}
			pythonArguments := []string{"test", bundle.Directory}
			pythonArguments = append(
				pythonArguments,
				"--framework", bundle.Config.Spec.Framework.Name,
				"--mode", bundle.Config.Spec.Framework.EffectiveMode(),
			)
			if includeSmoke {
				pythonArguments = append(pythonArguments, "--smoke")
			}
			if includeEvals {
				pythonArguments = append(
					pythonArguments,
					"--evals",
					"--eval-trajectory", evalTrajectory,
				)
			}
			return runPythonCLI(
				command.Context(), a, python, pythonArguments,
				configuredEnvironment(bundle), command.InOrStdin(),
				command.OutOrStdout(), command.ErrOrStderr(),
			)
		},
	}
	command.Flags().BoolVar(&includeSmoke, "smoke", false, "also run tests/smoke")
	command.Flags().BoolVar(&includeEvals, "evals", false, "run evals after Python tests pass")
	command.Flags().StringVar(
		&evalTrajectory,
		"eval-trajectory",
		"business",
		"eval tool trajectory: business or strict",
	)
	return command
}

func (a *application) newServeCommand() *cobra.Command {
	var output string
	var host string
	var port int
	var requestTimeout float64
	var maxConcurrency int
	var allowRemote bool
	command := &cobra.Command{
		Use:   "serve AGENT_DIR",
		Short: "Compile an agent and run its standalone HTTP server",
		Args:  cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			bundle, err := loadAgentBundle(arguments[0])
			if err != nil {
				return err
			}
			options := serveOptions{
				output: output, host: host, port: port,
				requestTimeout: requestTimeout, maxConcurrency: maxConcurrency,
				allowRemote: allowRemote,
				overrides: serveOverrides{
					host:           command.Flags().Changed("host"),
					port:           command.Flags().Changed("port"),
					requestTimeout: command.Flags().Changed("request-timeout"),
					maxConcurrency: command.Flags().Changed("max-concurrency"),
					allowRemote:    command.Flags().Changed("allow-remote"),
				},
			}
			if err := options.validate(); err != nil {
				return err
			}
			return a.serveBundle(command, bundle, options)
		},
	}
	command.Flags().StringVarP(&output, "output", "o", "", "retain the compiled artifact in this directory")
	command.Flags().StringVar(&host, "host", "127.0.0.1", "HTTP bind host")
	command.Flags().IntVar(&port, "port", 8080, "HTTP bind port")
	command.Flags().Float64Var(&requestTimeout, "request-timeout", 0, "non-streaming request deadline in seconds")
	command.Flags().IntVar(&maxConcurrency, "max-concurrency", 0, "maximum concurrent server connections")
	command.Flags().BoolVar(
		&allowRemote,
		"allow-remote",
		false,
		"allow a non-loopback bind (the server has no built-in authentication)",
	)
	return command
}

type serveOptions struct {
	output, host   string
	port           int
	requestTimeout float64
	maxConcurrency int
	allowRemote    bool
	overrides      serveOverrides
}

type serveOverrides struct {
	host, port, requestTimeout, maxConcurrency, allowRemote bool
}

func (o serveOptions) validate() error {
	if o.overrides.host && strings.TrimSpace(o.host) == "" {
		return fmt.Errorf("--host cannot be empty")
	}
	if o.overrides.port && (o.port < 1 || o.port > 65535) {
		return fmt.Errorf("--port must be between 1 and 65535")
	}
	if o.overrides.requestTimeout && o.requestTimeout <= 0 {
		return fmt.Errorf("--request-timeout must be greater than zero")
	}
	if o.overrides.maxConcurrency && o.maxConcurrency < 1 {
		return fmt.Errorf("--max-concurrency must be at least one")
	}
	return nil
}

func (a *application) serveBundle(command *cobra.Command, bundle engine.Bundle, options serveOptions) error {
	python, err := a.resolvePython()
	if err != nil {
		return err
	}
	artifact, cleanup, err := serveArtifactDirectory(options.output, bundle.Config.Metadata.Name)
	if err != nil {
		return err
	}
	defer cleanup()
	if err := a.compileForServe(command, python, bundle, artifact); err != nil {
		return err
	}
	launcher, err := compiledLauncher(artifact)
	if err != nil {
		return err
	}
	args := options.arguments(launcher)
	// The mutable server.yaml is authoritative unless an operator explicitly
	// supplied a serve flag, so the CLI must not advertise stale defaults.
	fmt.Fprintf(
		command.ErrOrStderr(),
		"Serving %s using %s; compiled server.yaml supplies HTTP defaults\n",
		bundle.Config.Metadata.Name, python.Executable,
	)
	server := a.system.commandContext(command.Context(), python.Executable, args...)
	server.Env = configuredEnvironment(bundle)
	if err := runCommand(server, command.InOrStdin(), command.OutOrStdout(), command.ErrOrStderr()); err != nil {
		return fmt.Errorf("run generated harnest-agent: %w", err)
	}
	return nil
}

func serveArtifactDirectory(output, name string) (string, func(), error) {
	if strings.TrimSpace(output) != "" {
		return output, func() {}, nil
	}
	root, err := os.MkdirTemp("", "harnest-serve-")
	if err != nil {
		return "", nil, fmt.Errorf("create temporary artifact root: %w", err)
	}
	return filepath.Join(root, name), func() { _ = os.RemoveAll(root) }, nil
}

func (a *application) compileForServe(command *cobra.Command, python pythonSelection, bundle engine.Bundle, artifact string) error {
	var output bytes.Buffer
	args := []string{"compile", bundle.Directory, "--output", artifact, "--entrypoint", bundle.Config.Spec.Entrypoint, "--framework", bundle.Config.Spec.Framework.Name, "--mode", bundle.Config.Spec.Framework.EffectiveMode()}
	return runPythonCLI(command.Context(), a, python, args, configuredEnvironment(bundle), command.InOrStdin(), &output, command.ErrOrStderr())
}

func compiledLauncher(artifact string) (string, error) {
	launcher := filepath.Join(artifact, "harnest-agent")
	info, err := os.Lstat(launcher)
	if err != nil {
		return "", fmt.Errorf("inspect generated launcher: %w", err)
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("generated launcher %s is not a regular file", launcher)
	}
	return launcher, nil
}

func (o serveOptions) arguments(launcher string) []string {
	args := []string{launcher}
	if o.overrides.host {
		args = append(args, "--host", o.host)
	}
	if o.overrides.port {
		args = append(args, "--port", strconv.Itoa(o.port))
	}
	if o.overrides.requestTimeout {
		args = append(args, "--request-timeout", strconv.FormatFloat(o.requestTimeout, 'g', -1, 64))
	}
	if o.overrides.maxConcurrency {
		args = append(args, "--max-concurrency", strconv.Itoa(o.maxConcurrency))
	}
	if o.overrides.allowRemote && o.allowRemote {
		args = append(args, "--allow-remote")
	}
	return args
}
