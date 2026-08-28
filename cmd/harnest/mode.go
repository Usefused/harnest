package main

import (
	"errors"
	"fmt"
	"io/fs"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/engine"
)

var managedResourceDirectories = []string{
	"tools",
	"subagents",
	"mcp",
	"extensions",
	"plugins",
	"sandbox",
	"skills",
	"evals",
}

var advancedExplicitResourceDirectories = []string{
	"tools", "subagents", "mcp", "plugins", "sandbox", "skills",
}

type populatedManagedResource struct {
	Name      string
	FileCount int
}

func (a *application) newModeCommand() *cobra.Command {
	command := &cobra.Command{
		Use:   "mode",
		Short: "Inspect authoring-mode migrations without changing the agent",
		Long: `Inspect the work required to move an existing agent between authoring modes.

Mode commands are audits only. They never rewrite config.yaml or agent.py and
never move, remove, or generate resource files.`,
	}
	command.AddCommand(a.newAdvancedModeCommand())
	return command
}

func (a *application) newAdvancedModeCommand() *cobra.Command {
	var check bool
	command := &cobra.Command{
		Use:   "advanced AGENT_DIR --check",
		Short: "Audit an agent before switching it to advanced mode",
		Long: `Report the current framework, mode, entrypoint, and managed resource
directories that must be wired explicitly in advanced mode. This command is
strictly read-only; make the reported source and config changes yourself or
with a coding agent after reviewing the audit.`,
		Args: cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			if !check {
				return fmt.Errorf("advanced-mode migration is read-only; pass --check to inspect required changes")
			}
			bundle, err := engine.LoadBundle(arguments[0])
			if err != nil {
				return fmt.Errorf("inspect agent for advanced mode: %w", err)
			}
			resources, err := findPopulatedManagedResources(bundle.Directory)
			if err != nil {
				return err
			}
			writeAdvancedModeAudit(command, bundle, resources)
			return nil
		},
	}
	command.Flags().BoolVar(
		&check,
		"check",
		false,
		"report required migration work without changing any files",
	)
	return command
}

func findPopulatedManagedResources(root string) ([]populatedManagedResource, error) {
	resources := make([]populatedManagedResource, 0, len(advancedExplicitResourceDirectories))
	for _, name := range advancedExplicitResourceDirectories {
		directory := filepath.Join(root, name)
		count := 0
		err := filepath.WalkDir(directory, countManagedResourceFiles(directory, &count))
		if err != nil {
			return nil, fmt.Errorf("inspect managed resource directory %s: %w", directory, err)
		}
		if count > 0 {
			resources = append(resources, populatedManagedResource{Name: name, FileCount: count})
		}
	}
	return resources, nil
}

func countManagedResourceFiles(directory string, count *int) fs.WalkDirFunc {
	return func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			if path == directory && errors.Is(walkErr, fs.ErrNotExist) {
				return nil
			}
			return walkErr
		}
		if path == directory {
			return nil
		}
		if isPlaceholderResourceEntry(entry.Name()) {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if !entry.IsDir() {
			*count++
		}
		return nil
	}
}

func isPlaceholderResourceEntry(name string) bool {
	return strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_")
}

// writeAdvancedModeAudit separates retained Harnest governance from native
// responsibilities so migration advice cannot imply that source was rewritten.
func writeAdvancedModeAudit(
	command *cobra.Command,
	bundle engine.Bundle,
	resources []populatedManagedResource,
) {
	output := command.OutOrStdout()
	fmt.Fprintln(output, "Advanced-mode migration check (read-only)")
	fmt.Fprintf(output, "Agent directory: %s\n", bundle.Directory)
	fmt.Fprintf(output, "Framework: %s\n", bundle.Config.Spec.Framework.Name)
	fmt.Fprintf(output, "Current mode: %s\n", bundle.Config.Spec.Framework.EffectiveMode())
	fmt.Fprintf(output, "Entrypoint: %s\n", bundle.Config.Spec.Entrypoint)
	fmt.Fprintln(output, "Managed resource directories requiring explicit wiring:")
	if len(resources) == 0 {
		fmt.Fprintln(output, "  none")
	} else {
		for _, resource := range resources {
			label := "files"
			if resource.FileCount == 1 {
				label = "file"
			}
			fmt.Fprintf(output, "  - %s/ (%d %s)\n", resource.Name, resource.FileCount, label)
		}
	}
	fmt.Fprintln(output, "Harnest still owns:")
	fmt.Fprintln(output, "  - neutral HTTP/SSE/WebSocket serving and server.yaml policy")
	fmt.Fprintln(output, "  - decorated authentication lifecycle and principal-scoped sessions")
	fmt.Fprintln(output, "  - approval and tracing context for explicitly decorated native capabilities")
	fmt.Fprintln(output, "  - portable extension hooks around neutral invocations")
	fmt.Fprintln(output, "You own in advanced mode:")
	fmt.Fprintln(output, "  - native graph routing, state, checkpoint, and framework semantics")
	fmt.Fprintln(output, "  - native middleware/plugin registration and framework upgrades")
	fmt.Fprintln(output, "  - native object validation beyond Harnest's entrypoint checks")
	fmt.Fprintln(output, "  - native tool and MCP declaration; opaque capabilities are not discovered automatically")
	fmt.Fprintln(output, "  - arbitrary native model calls; portable model hooks are not auto-injected into advanced targets, so use a LiteLLM lifecycle or native plugin/middleware")

	fmt.Fprintln(output, "Next steps:")
	fmt.Fprintln(output, "  1. Preserve agent.py and expose the framework target with Agent.advanced(...).")
	if len(resources) > 0 {
		fmt.Fprintln(output, "  2. Wire every listed runtime resource explicitly from agent.py; keep evals as test-only inputs.")
	} else {
		fmt.Fprintln(output, "  2. Confirm agent.py owns every framework-specific dependency and lifecycle hook.")
	}
	if bundle.Config.Spec.Framework.EffectiveMode() == "advanced" {
		fmt.Fprintln(output, "  3. Keep spec.framework.mode set to advanced after reviewing the wiring.")
	} else {
		fmt.Fprintln(output, "  3. Set spec.framework.mode to advanced only after the entrypoint is ready.")
	}
	fmt.Fprintf(output, "  4. Run harnest test %s, then harnest compile %s.\n", bundle.Directory, bundle.Directory)
	fmt.Fprintln(output, "No files were changed.")
}
