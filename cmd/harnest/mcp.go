package main

import "github.com/spf13/cobra"

// newMCPCommand inspects authored connections without compiling or invoking a model.
func (a *application) newMCPCommand() *cobra.Command {
	command := &cobra.Command{Use: "mcp", Short: "Discover MCP tools, resources, and prompts"}
	command.AddCommand(a.newMCPOperation("inspect CLIENT", 1), a.newMCPOperation("read CLIENT URI", 2), a.newMCPOperation("prompt CLIENT NAME", 2))
	return command
}

// newMCPOperation keeps all reads on the selected agent's runtime and credentials.
func (a *application) newMCPOperation(use string, count int) *cobra.Command {
	var project string
	var jsonOutput bool
	var promptArgs []string
	var catalog, cursor string
	command := &cobra.Command{
		Use: use, Short: "Query a configured MCP connection", Args: cobra.ExactArgs(count),
		RunE: func(command *cobra.Command, arguments []string) error {
			bundle, err := loadAgentBundle(project)
			if err != nil {
				return err
			}
			python, err := a.agentPython(command, bundle, runtimeEnvironmentProfile)
			if err != nil {
				return err
			}
			defer python.releaseLease()
			args := append([]string{"mcp", command.Name()}, arguments...)
			args = append(args, "--project", bundle.Directory, "--framework", bundle.Config.Spec.Framework.Name)
			if jsonOutput {
				args = append(args, "--json")
			}
			for _, value := range promptArgs {
				args = append(args, "--arg", value)
			}
			if catalog != "" {
				args = append(args, "--catalog", catalog)
			}
			if cursor != "" {
				args = append(args, "--cursor", cursor)
			}
			return runPythonCLI(command.Context(), a, python, args, configuredEnvironment(bundle), command.InOrStdin(), command.OutOrStdout(), command.ErrOrStderr())
		},
	}
	command.Flags().StringVar(&project, "project", ".", "agent directory (default: current folder)")
	command.Flags().BoolVar(&jsonOutput, "json", false, "emit compact machine-readable JSON")
	if command.Name() == "prompt" {
		command.Flags().StringArrayVar(&promptArgs, "arg", nil, "prompt argument key=value (repeatable)")
	}
	if command.Name() == "inspect" {
		command.Flags().StringVar(&catalog, "catalog", "", "list one catalog: tools, resources, resource-templates, or prompts")
		command.Flags().StringVar(&cursor, "cursor", "", "nextCursor from the selected catalog")
	}
	return command
}
