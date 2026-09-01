package main

import (
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/engine"
)

const maxRunMessageBytes = 4 * 1024 * 1024

// newRunCommand invokes a compiled agent locally without introducing an HTTP boundary.
func (a *application) newRunCommand() *cobra.Command {
	var session string
	var output string
	command := &cobra.Command{
		Use:   "run AGENT_DIR [MESSAGE]",
		Short: "Compile an agent and invoke it locally",
		Args:  cobra.RangeArgs(1, 2),
		RunE: func(command *cobra.Command, arguments []string) error {
			message, err := resolveRunMessage(arguments[1:], command.InOrStdin())
			if err != nil {
				return err
			}
			options := runOptions{session: session, output: output}
			if err := options.validate(command.Flags().Changed("session")); err != nil {
				return err
			}
			bundle, err := loadAgentBundle(arguments[0])
			if err != nil {
				return err
			}
			if !bundle.Config.Spec.Interfaces.CLI {
				return fmt.Errorf(
					"agent CLI is disabled; set spec.interfaces.cli: true in config.yaml",
				)
			}
			return a.runBundle(command, bundle, message, options)
		},
	}
	command.Flags().StringVar(&session, "session", "", "persistent session identifier")
	command.Flags().StringVar(&output, "output", "text", "output format: text, json, or ndjson")
	return command
}

type runOptions struct {
	session string
	output  string
}

// validate rejects ambiguous values before compilation creates an artifact.
func (o runOptions) validate(sessionChanged bool) error {
	if sessionChanged && strings.TrimSpace(o.session) == "" {
		return fmt.Errorf("--session cannot be empty")
	}
	switch o.output {
	case "text", "json", "ndjson":
		return nil
	default:
		return fmt.Errorf("--output must be text, json, or ndjson")
	}
}

// arguments keeps the prompt off argv so local process inspection cannot expose it.
func (o runOptions) arguments(launcher string) []string {
	arguments := []string{launcher, "run"}
	if strings.TrimSpace(o.session) != "" {
		arguments = append(arguments, "--session", o.session)
	}
	return append(arguments, "--output", o.output)
}

// resolveRunMessage accepts exactly one input source while treating terminal stdin as absent.
func resolveRunMessage(positional []string, stdin io.Reader) (string, error) {
	if len(positional) == 0 {
		return readRequiredRunMessage(stdin)
	}
	message := positional[0]
	if strings.TrimSpace(message) == "" {
		return "", fmt.Errorf("MESSAGE cannot be empty")
	}
	provided, err := stdinHasRunMessage(stdin)
	if err != nil {
		return "", err
	}
	if provided {
		return "", fmt.Errorf("MESSAGE and stdin are mutually exclusive")
	}
	return message, nil
}

// stdinHasRunMessage avoids reading an interactive terminal, which would block positional use.
func stdinHasRunMessage(stdin io.Reader) (bool, error) {
	if file, ok := stdin.(*os.File); ok {
		info, err := file.Stat()
		if err != nil {
			return false, fmt.Errorf("inspect stdin: %w", err)
		}
		if info.Mode()&os.ModeCharDevice != 0 {
			return false, nil
		}
	}
	contents, err := readBoundedRunMessage(stdin)
	if err != nil {
		return false, fmt.Errorf("read MESSAGE from stdin: %w", err)
	}
	return strings.TrimSpace(string(contents)) != "", nil
}

// readRequiredRunMessage normalizes the conventional trailing newline from shell pipes.
func readRequiredRunMessage(stdin io.Reader) (string, error) {
	contents, err := readBoundedRunMessage(stdin)
	if err != nil {
		return "", fmt.Errorf("read MESSAGE from stdin: %w", err)
	}
	message := string(contents)
	if strings.TrimSpace(message) == "" {
		return "", fmt.Errorf("MESSAGE is required as an argument or on stdin")
	}
	message = strings.TrimSuffix(message, "\n")
	return strings.TrimSuffix(message, "\r"), nil
}

// readBoundedRunMessage applies the same local-input ceiling as the generated runtime.
func readBoundedRunMessage(stdin io.Reader) ([]byte, error) {
	contents, err := io.ReadAll(io.LimitReader(stdin, maxRunMessageBytes+1))
	if err != nil {
		return nil, err
	}
	if len(contents) > maxRunMessageBytes {
		return nil, fmt.Errorf("MESSAGE exceeds the 4 MiB limit")
	}
	return contents, nil
}

// runBundle compiles ephemerally and invokes the same generated runtime used by deployments.
func (a *application) runBundle(
	command *cobra.Command, bundle engine.Bundle, message string, options runOptions,
) error {
	python, err := a.agentPython(command, bundle)
	if err != nil {
		return err
	}
	artifact, cleanup, err := compiledArtifactDirectory("", bundle.Config.Metadata.Name, "harnest-run-")
	if err != nil {
		return err
	}
	defer cleanup()
	if err := a.compileBundle(command, python, bundle, artifact, strings.NewReader("")); err != nil {
		return err
	}
	launcher, err := compiledLauncher(artifact)
	if err != nil {
		return err
	}
	runner := a.system.commandContext(command.Context(), python.Executable, options.arguments(launcher)...)
	runner.Env = configuredEnvironment(bundle)
	if err := runCommand(
		runner, strings.NewReader(message), command.OutOrStdout(), command.ErrOrStderr(),
	); err != nil {
		return fmt.Errorf("run generated harnest-agent: %w", err)
	}
	return nil
}
