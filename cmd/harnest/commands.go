package main

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
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
	command := &cobra.Command{
		Use:   "test AGENT_DIR",
		Short: "Compile an agent and run its authored tests",
		Args:  cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
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
				pythonArguments = append(pythonArguments, "--evals")
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
			if strings.TrimSpace(host) == "" {
				return fmt.Errorf("--host cannot be empty")
			}
			if port < 1 || port > 65535 {
				return fmt.Errorf("--port must be between 1 and 65535")
			}
			bundle, err := loadAgentBundle(arguments[0])
			if err != nil {
				return err
			}
			if !command.Flags().Changed("request-timeout") {
				requestTimeout = float64(bundle.Config.Spec.Resources.TimeoutSeconds)
				if requestTimeout == 0 {
					requestTimeout = 300
				}
			}
			if requestTimeout <= 0 {
				return fmt.Errorf("--request-timeout must be greater than zero")
			}
			if !command.Flags().Changed("max-concurrency") {
				maxConcurrency = bundle.Config.Spec.Resources.MaxConcurrentRequests
				if maxConcurrency == 0 {
					maxConcurrency = 8
				}
			}
			if maxConcurrency < 1 {
				return fmt.Errorf("--max-concurrency must be at least one")
			}

			python, err := a.resolvePython()
			if err != nil {
				return err
			}
			artifactDirectory := output
			cleanup := func() {}
			if strings.TrimSpace(artifactDirectory) == "" {
				temporaryRoot, err := os.MkdirTemp("", "harnest-serve-")
				if err != nil {
					return fmt.Errorf("create temporary artifact root: %w", err)
				}
				cleanup = func() { _ = os.RemoveAll(temporaryRoot) }
				artifactDirectory = filepath.Join(temporaryRoot, bundle.Config.Metadata.Name)
			}
			defer cleanup()

			var compileOutput bytes.Buffer
			if err := runPythonCLI(
				command.Context(),
				a,
				python,
				[]string{
					"compile", bundle.Directory, "--output", artifactDirectory,
					"--entrypoint", bundle.Config.Spec.Entrypoint,
					"--framework", bundle.Config.Spec.Framework.Name,
					"--mode", bundle.Config.Spec.Framework.EffectiveMode(),
				},
				configuredEnvironment(bundle),
				command.InOrStdin(),
				&compileOutput,
				command.ErrOrStderr(),
			); err != nil {
				return err
			}
			launcher := filepath.Join(artifactDirectory, "harnest-agent")
			info, err := os.Lstat(launcher)
			if err != nil {
				return fmt.Errorf("inspect generated launcher: %w", err)
			}
			if !info.Mode().IsRegular() {
				return fmt.Errorf("generated launcher %s is not a regular file", launcher)
			}

			serveArguments := []string{
				launcher,
				"--host", host,
				"--port", strconv.Itoa(port),
				"--request-timeout", strconv.FormatFloat(requestTimeout, 'g', -1, 64),
				"--max-concurrency", strconv.Itoa(maxConcurrency),
			}
			if allowRemote {
				serveArguments = append(serveArguments, "--allow-remote")
			}
			fmt.Fprintf(
				command.ErrOrStderr(),
				"Serving %s on http://%s:%d using %s\n",
				bundle.Config.Metadata.Name,
				host,
				port,
				python.Executable,
			)
			server := a.system.commandContext(command.Context(), python.Executable, serveArguments...)
			server.Env = configuredEnvironment(bundle)
			if err := runCommand(
				server, command.InOrStdin(), command.OutOrStdout(), command.ErrOrStderr(),
			); err != nil {
				return fmt.Errorf("run generated harnest-agent: %w", err)
			}
			return nil
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
