// `harnest pack` and `harnest pack init NAME`: scaffolds a new Harnest pack (pack.yaml and pack.py).

package main

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"
)

// newPackCommand groups everything a company needs to author, check and ship one pack.
func (a *application) newPackCommand() *cobra.Command {
	var output string
	command := &cobra.Command{
		Use:   "pack",
		Short: "Create, validate, build and run company packs",
		Long: `A pack is a folder with pack.yaml (name, version, branded command and an
optional studio sub-pack) and an optional pack.py (init and migration hooks and
company commands). Use it with harnest init|upgrade|studio --pack DIR, or build
it into a branded command with harnest pack build DIR.`,
	}
	initialize := &cobra.Command{
		Use: "init NAME", Short: "Generate a pack with pack.yaml, pack.py and templates", Args: cobra.ExactArgs(1),
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
			fmt.Fprintf(command.OutOrStdout(),
				"Pack: %s\nManifest: %s\nHooks: %s\nCompile declarations: %s\n\nTry it: harnest init ./my-agent --pack %s\nBuild the %s command: harnest pack build %s\n",
				absolute, filepath.Join(absolute, packManifestFile), filepath.Join(absolute, "pack.py"),
				filepath.Join(absolute, "templates", compileSelectionFile), absolute, name, absolute)
			return nil
		},
	}
	initialize.Flags().StringVarP(&output, "output", "o", "", "new pack directory (default: NAME-pack)")
	command.AddCommand(initialize, a.newPackValidateCommand(), a.newPackBuildCommand(),
		a.newPackRunCommand(), a.newPackTypesCommand())
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
	if err = os.MkdirAll(filepath.Join(stage, "templates"), 0755); err != nil {
		return err
	}
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

// projectPackScaffoldFiles writes a pack that already works with harnest init --pack.
func projectPackScaffoldFiles(name string) map[string]string {
	return map[string]string{
		packManifestFile: projectPackManifestScaffold(name),
		"pack.py":        projectPackPythonScaffold(name),
		filepath.ToSlash(filepath.Join("templates", compileSelectionFile)): "# Copied into each new agent by pack.py.\nversion: 1\n# Select optional groups declared in the agent's pyproject.toml.\nextras: []\n",
		"templates/team-guide.md": "# " + name + " team reference\n\nReplace this sample with setup steps, usage examples, or troubleshooting for teammates.\nThis guide stays in the generated project and is not compiled.\nUse an agent template to provide instructions.md or skills for agent-facing guidance.\n",
	}
}

// projectPackManifestScaffold declares identity; Studio reads it without running pack code.
func projectPackManifestScaffold(name string) string {
	return fmt.Sprintf(`apiVersion: harnest.dev/pack/v1
name: %s
version: 0.1.0
title: %s agents
description: Company defaults for new %s agents.
# Branded command built by harnest pack build (defaults to the pack name).
command: %s
# Optional Studio sub-pack: builder defaults, UI and installable resources.
# studio:
#   resources:
#     - id: support-template
#       kind: template
#       title: Support agent
#       files:
#         agent-card.yaml: studio/support/agent-card.yaml
`, name, name, name, name)
}

// projectPackPythonScaffold uses the public pack API; pack.yaml supplies the name.
func projectPackPythonScaffold(name string) string {
	return fmt.Sprintf(`"""Project hooks for the %s pack. pack.yaml holds its name, version and command."""
from harnest.authoring import ChangePlan, ProjectContext, ProjectPack


pack = ProjectPack(schema_version=1, templates="templates")


@pack.initialize
def initialize(context: ProjectContext) -> ChangePlan:
    """Copy team documentation and compile declarations into each new agent."""
    return ChangePlan(
        context.files.from_file("docs/team-guide.md", source="team-guide.md"),
        context.files.from_template("harnest-compile.yaml", template="harnest-compile.yaml"),
    )


# For existing agents, increment schema_version and add a consecutive
# @pack.migration(from_version=1, to_version=2). Upgrades never rerun initialize.
#
# Company commands become subcommands of the built %s command:
#
# @pack.command("check", help="Check this agent against %s standards")
# def check(args: list[str]) -> int:
#     return 0
`, name, name, name)
}
