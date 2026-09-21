package main

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
)

type addChannelOptions struct {
	Project string
	Via     string
}

// newAddChannelCommand scaffolds one local channel binding without contacting a provider.
func (a *application) newAddChannelCommand() *cobra.Command {
	options := addChannelOptions{}
	command := &cobra.Command{
		Use:   "channel PLATFORM --via EXTENSION",
		Short: "Add a chat-platform channel binding",
		Long: `Add a local channel binding naming the platform and the extension that owns its transport.

The binding declares policy only: which platform, which extension, and which
installations or conversations are authorized. It does not create external
apps, expand scopes, or write credentials; the named extension owns those.`,
		Example: `  harnest add channel slack --via fused
  harnest add channel slack --via direct`,
		Args: cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			if err := options.validate(); err != nil {
				return err
			}
			created, normalized, err := addChannelResource(options, arguments[0])
			if err != nil {
				return err
			}
			fmt.Fprintf(command.OutOrStdout(), "Added channel %q at %s\n", normalized, created)
			fmt.Fprintln(command.OutOrStdout(), "Next: run `harnest channels inspect .` from the agent folder")
			return nil
		},
	}
	flags := command.Flags()
	flags.StringVar(&options.Project, "project", ".", "Harnest agent root containing config.yaml")
	flags.StringVar(&options.Via, "via", "", "channel adapter extension that owns this platform's transport")
	return command
}

// validate rejects an unnamed extension before any filesystem write.
func (options addChannelOptions) validate() error {
	if strings.TrimSpace(options.Via) == "" {
		return fmt.Errorf("--via must name the channel adapter extension for this platform")
	}
	if !agentResourceNamePattern.MatchString(options.Via) {
		return fmt.Errorf("--via must begin with a lowercase letter and contain only lowercase letters, numbers, hyphens, or underscores")
	}
	return nil
}

// addChannelResource reuses the standard project, mode, path, and collision policy.
func addChannelResource(options addChannelOptions, requestedName string) (string, string, error) {
	scaffold := agentResourceScaffold{
		Kind: "channel", Directory: "channels", ManagedOnly: true,
		Source: func(name string) string {
			return channelResourceSource(name, options)
		},
	}
	return addAgentResource(options.Project, requestedName, scaffold)
}

// channelResourceSource emits only a provider-neutral policy declaration.
func channelResourceSource(name string, options addChannelOptions) string {
	return fmt.Sprintf(`"""Bind the %s channel to this agent."""

from harnest.channels import ChannelBinding


def binding() -> ChannelBinding:
    """Declare the %s channel binding for the %s extension."""

    return ChannelBinding(
        platform=%s,
        extension=%s,
    )
`, strings.ReplaceAll(name, "_", " "), strings.ReplaceAll(name, "_", " "), options.Via,
		strconv.Quote(name), strconv.Quote(options.Via))
}

// newChannelsCommand inspects and fixture-tests authored bindings without a live provider.
func (a *application) newChannelsCommand() *cobra.Command {
	command := &cobra.Command{Use: "channels", Short: "Inspect and fixture-test configured channel bindings"}
	command.AddCommand(a.newChannelsOperation("inspect"), a.newChannelsOperation("test"))
	return command
}

// newChannelsOperation keeps discovery and fixture parsing offline and model-free.
func (a *application) newChannelsOperation(operation string) *cobra.Command {
	var platform string
	var fixture string
	var jsonOutput bool
	command := &cobra.Command{
		Use:   operation + " AGENT_DIR",
		Short: channelsOperationShort(operation),
		Args:  cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			bundle, err := loadAgentBundle(arguments[0])
			if err != nil {
				return err
			}
			python, err := a.agentPython(command, bundle, runtimeEnvironmentProfile)
			if err != nil {
				return err
			}
			defer python.releaseLease()
			args := []string{"channels", operation}
			if platform != "" {
				args = append(args, platform)
			}
			args = append(args, "--project", bundle.Directory)
			if fixture != "" {
				args = append(args, "--fixture", fixture)
			}
			if jsonOutput {
				args = append(args, "--json")
			}
			return runPythonCLI(command.Context(), a, python, args, configuredEnvironment(bundle), command.InOrStdin(), command.OutOrStdout(), command.ErrOrStderr())
		},
	}
	command.Flags().StringVar(&platform, "platform", "", "inspect or test only this platform's binding")
	command.Flags().BoolVar(&jsonOutput, "json", false, "emit compact machine-readable JSON")
	if operation == "test" {
		command.Flags().StringVar(&fixture, "fixture", "", "offline provider payload normalized without sending")
	}
	return command
}

// channelsOperationShort keeps the two subcommands' help text self-explanatory.
func channelsOperationShort(operation string) string {
	if operation == "inspect" {
		return "Report configured bindings and extension readiness"
	}
	return "Normalize an offline fixture through a binding's adapter"
}
