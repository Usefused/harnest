package main

import (
	"fmt"

	"github.com/spf13/cobra"
	"harnest.dev/harnest/engine"
)

// newAddCronCommand creates one scheduled function without a separate task file.
func (a *application) newAddCronCommand() *cobra.Command {
	var project, schedule string
	var dynamic bool
	command := &cobra.Command{
		Use:   "cron NAME",
		Short: "Add a scheduled function with its own queued task",
		Long: `Add a @cron function in cron/ for ADK or LangGraph, in either authoring mode.
The function owns its queued task. Configure task and cron storage before serving.
The default fixed schedule runs daily at 09:00 UTC; --dynamic creates a target
whose schedule is created later with harnest.cron.create.`,
		Example: "  harnest add cron daily-report --schedule '0 9 * * 1-5'\n  harnest add cron reminder --dynamic",
		Args:    cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			if dynamic && command.Flags().Changed("schedule") {
				return fmt.Errorf("--dynamic and --schedule cannot be combined")
			}
			if !dynamic {
				if err := engine.ValidateCronSchedule(schedule); err != nil {
					return fmt.Errorf("--schedule: %w", err)
				}
			}
			scaffold := agentResourceScaffold{Kind: "cron", Directory: "cron", Source: func(name string) string {
				return cronResourceSource(name, schedule, dynamic)
			}}
			created, name, err := addAgentResource(project, arguments[0], scaffold)
			if err != nil {
				return err
			}
			fmt.Fprintf(command.OutOrStdout(), "Added cron %q at %s\n", name, created)
			fmt.Fprintln(command.OutOrStdout(), "Next: implement the function and configure shared task/cron storage before serving.")
			return nil
		},
	}
	command.Flags().StringVar(&project, "project", ".", "Harnest agent root containing config.yaml")
	command.Flags().StringVar(&schedule, "schedule", "0 9 * * *", "five-column UTC cron expression")
	command.Flags().BoolVar(&dynamic, "dynamic", false, "create a target without a fixed schedule")
	return command
}

// cronResourceSource keeps fixed and dynamic targets on the same task contract.
func cronResourceSource(name, schedule string, dynamic bool) string {
	options := `queue="default", max_retries=3`
	if !dynamic {
		options = fmt.Sprintf("%q, %s", schedule, options)
	}
	return fmt.Sprintf(`from harnest.cron import cron


@cron(%s)
async def %s() -> dict[str, str]:
    """Perform the scheduled work and return a serializable result."""
    return {"status": "completed"}
`, options, name)
}
