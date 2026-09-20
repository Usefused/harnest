package main

import (
	"strconv"

	"github.com/spf13/cobra"
)

// newProvisionCommand exposes the shared Python provisioner without importing agent code.
func (a *application) newProvisionCommand() *cobra.Command {
	command := &cobra.Command{Use: "provision", Short: "Deploy agent images and services locally or to Kubernetes"}
	for _, operation := range []string{"init", "plan", "apply", "status", "stop", "remove", "history", "rollback"} {
		command.AddCommand(a.newProvisionOperation(operation))
	}
	return command
}

// newProvisionOperation binds one finite lifecycle action to an explicit project and environment.
func (a *application) newProvisionOperation(operation string) *cobra.Command {
	var project, environment string
	var revision, limit, before int
	command := &cobra.Command{
		Use: operation, Short: operation + " the configured agent and service deployment", Args: cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			python, err := a.resolvePython()
			if err != nil {
				return err
			}
			args := []string{"-m", "harnest.provisioner_cli", operation, "--project", project, "--environment", environment}
			if command.Flags().Changed("revision") {
				args = append(args, "--revision", strconv.Itoa(revision))
			}
			if command.Flags().Changed("limit") {
				args = append(args, "--limit", strconv.Itoa(limit))
			}
			if command.Flags().Changed("before-revision") {
				args = append(args, "--before-revision", strconv.Itoa(before))
			}
			process := a.system.commandContext(command.Context(), python.Executable, args...)
			return runCommand(process, command.InOrStdin(), command.OutOrStdout(), command.ErrOrStderr())
		},
	}
	command.Flags().IntVar(&revision, "revision", 0, "successful revision to preview or roll back to")
	command.Flags().IntVar(&limit, "limit", 20, "maximum history entries (1-100)")
	command.Flags().IntVar(&before, "before-revision", 0, "history pagination cursor")
	command.Flags().StringVar(&project, "project", ".", "project containing harnest-deployment.yaml")
	command.Flags().StringVar(&environment, "environment", "local", "deployment environment overlay")
	return command
}
