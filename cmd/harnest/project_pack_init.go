package main

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"
)

// newPackCommand scaffolds authoring packs without adding a general-purpose Python compiler.
func (a *application) newPackCommand() *cobra.Command {
	var output string
	command := &cobra.Command{Use: "pack", Short: "Create company project packs"}
	initialize := &cobra.Command{
		Use: "init NAME", Short: "Generate a Python project pack and compile declarations", Args: cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			name := arguments[0]
			destination := output
			if destination == "" {
				destination = name + "-pack"
			}
			absolute, err := filepath.Abs(destination)
			if err != nil {
				return err
			}
			if err = createProjectPackScaffold(absolute, name); err != nil {
				return err
			}
			fmt.Fprintf(command.OutOrStdout(), "Project pack: %s\nPython CLI: %s\nCompile declarations: %s\n", absolute, filepath.Join(absolute, "pack.py"), filepath.Join(absolute, compileSelectionFile))
			return nil
		},
	}
	initialize.Flags().StringVarP(&output, "output", "o", "", "new pack directory (default: NAME-pack)")
	command.AddCommand(initialize)
	return command
}

// createProjectPackScaffold publishes complete templates without replacing an existing team package.
func createProjectPackScaffold(destination, name string) error {
	if !scaffoldNamePattern.MatchString(name) || name == "harnest" {
		return fmt.Errorf("pack name must be a non-reserved kebab-case identifier (1-63 characters); harnest is reserved")
	}
	if err := requireNewProjectPack(destination); err != nil {
		return err
	}
	parent := filepath.Dir(destination)
	if err := os.MkdirAll(parent, 0755); err != nil {
		return err
	}
	stage, err := os.MkdirTemp(parent, ".harnest-pack-init-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(stage)
	files := projectPackScaffoldFiles(name)
	created := []string{}
	if err = createScaffoldFiles(stage, files, &created); err != nil {
		return err
	}
	if err = os.Chmod(stage, 0755); err != nil {
		return err
	}
	// An existing destination belongs to the team, including an empty directory or symlink.
	if err = requireNewProjectPack(destination); err != nil {
		return err
	}
	return os.Rename(stage, destination)
}

// requireNewProjectPack refuses accidental overwrites rather than offering destructive scaffold updates.
func requireNewProjectPack(destination string) error {
	_, err := os.Lstat(destination)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	return fmt.Errorf("project pack destination already exists: %s; choose a new directory", destination)
}

// projectPackScaffoldFiles keeps the editable compile manifest beside its Python authoring entrypoint.
func projectPackScaffoldFiles(name string) map[string]string {
	return map[string]string{
		"pack.py":            projectPackPythonScaffold(name),
		compileSelectionFile: "# Copied into each new agent by pack.py.\nversion: 1\n# Select optional groups declared in the agent's pyproject.toml.\nextras: []\n",
		"team-guide.md":      "# " + name + " team reference\n\nReplace this sample with setup steps, usage examples, or troubleshooting for teammates.\nThis guide stays in the generated project and is not compiled.\nUse an agent template to provide instructions.md or skills for agent-facing guidance.\n",
	}
}

// projectPackPythonScaffold uses the public pack API and copies templates without running build hooks.
func projectPackPythonScaffold(name string) string {
	return fmt.Sprintf(`"""Company project pack and CLI; keep this directory outside generated agents."""
from pathlib import Path
import os

from harnest.authoring import ChangePlan, ProjectCLI, ProjectContext, ProjectPack


pack = ProjectPack(%q, schema_version=1, templates=Path(__file__).parent)


@pack.initialize
def initialize(context: ProjectContext) -> ChangePlan:
    """Copy team documentation and compile declarations into each new agent."""
    return ChangePlan(
        context.files.from_file(
            "docs/team-guide.md", source="team-guide.md",
        ),
        context.files.from_template(
            "harnest-compile.yaml", template="harnest-compile.yaml",
        ),
    )


# For existing agents, increment schema_version and add a consecutive
# @pack.migration. Initializers are not replayed by upgrade.

def main() -> int:
    """Expose init and upgrade through the company CLI."""
    return ProjectCLI(
        %q, [pack],
        harnest_command=(os.environ.get("HARNEST_CLI", "harnest"),),
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
`, name, name+"-agent")
}
