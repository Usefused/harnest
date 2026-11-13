// `harnest upgrade`: migrates an agent to the current Harnest layout and runs pack upgrades.

package main

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"
)

// newUpgradeCommand separates required rewrites from optional authoring guidance.
func (a *application) newUpgradeCommand() *cobra.Command {
	var apply, asJSON bool
	var packs, options []string
	command := &cobra.Command{
		Use:   "upgrade AGENT_DIR",
		Short: "Plan or apply an existing agent repository migration",
		Long: `Inspect an existing Harnest agent for removed filesystem and Python
contracts and optional authoring migrations. Supported Cron(...) declarations
receive migration guidance and remain unchanged. Without --apply this command
is read-only. With --apply it prints the
same migration classes, backs up every changed source under .harnest, and then
performs the explicitly requested destructive moves and rewrites.

With --pack, the same core migration is combined with each pack's own
migrations into one reviewed plan. Without --pack, pack migrations are not run.`,
		Args: cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			if len(packs) > 0 {
				return a.runPackUpgrade(command, arguments[0], packs, options, apply, asJSON)
			}
			for _, flag := range []string{"option", "json"} {
				if command.Flags().Changed(flag) {
					return fmt.Errorf("--%s requires --pack", flag)
				}
			}
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
	command.Flags().StringArrayVar(&packs, "pack", nil, "also run this company pack's migrations (repeatable)")
	command.Flags().StringArrayVar(&options, "option", nil, "pack option as key=value, or pack.key=value with several packs (repeatable)")
	command.Flags().BoolVar(&asJSON, "json", false, "with --pack: print the plan as JSON")
	return command
}

// runPackUpgrade plans core and pack migrations together in a staging copy.
func (a *application) runPackUpgrade(
	command *cobra.Command, directory string, packs, options []string, apply, asJSON bool,
) error {
	folders, err := resolvePackFolders(packs)
	if err != nil {
		return err
	}
	absolute, err := filepath.Abs(directory)
	if err != nil {
		return err
	}
	executable, err := os.Executable()
	if err != nil {
		return fmt.Errorf("resolve Harnest executable: %w", err)
	}
	arguments := packPlanArguments("upgrade", absolute, executable, folders, options)
	if apply {
		arguments = append(arguments, "--apply")
	}
	if asJSON {
		arguments = append(arguments, "--json")
	}
	return a.runPackPython(command, arguments)
}
