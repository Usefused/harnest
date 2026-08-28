package main

import (
	"github.com/spf13/cobra"
)

func (a *application) newUpgradeCommand() *cobra.Command {
	var apply bool
	command := &cobra.Command{
		Use:   "upgrade AGENT_DIR",
		Short: "Plan or apply an existing agent repository migration",
		Long: `Inspect an existing Harnest agent for removed filesystem and Python
contracts. Without --apply this command is read-only. With --apply it prints the
same migration classes, backs up every changed source under .harnest, and then
performs the explicitly requested destructive moves and rewrites.`,
		Args: cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			python, err := a.resolvePython()
			if err != nil {
				return err
			}
			pythonArguments := []string{"upgrade", arguments[0]}
			if apply {
				pythonArguments = append(pythonArguments, "--apply")
			}
			return runPythonCLI(
				command.Context(), a, python, pythonArguments, nil,
				command.InOrStdin(), command.OutOrStdout(), command.ErrOrStderr(),
			)
		},
	}
	command.Flags().BoolVar(
		&apply,
		"apply",
		false,
		"apply the displayed destructive changes after creating backups",
	)
	return command
}
