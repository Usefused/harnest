// `harnest pack validate|build|run|types`: pack commands that run pack code in Python.

package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/spf13/cobra"
)

const packManifestFile = "pack.yaml"

// resolvePackFolders turns --pack values into absolute pack folders and points
// authors of the removed formats at the merged layout before Python starts.
func resolvePackFolders(packs []string) ([]string, error) {
	folders := make([]string, 0, len(packs))
	for _, path := range packs {
		absolute, err := filepath.Abs(path)
		if err != nil {
			return nil, err
		}
		if info, err := os.Stat(filepath.Join(absolute, "studio-pack.yaml")); err == nil && info.Mode().IsRegular() {
			return nil, fmt.Errorf("%s: studio-pack.yaml is no longer supported; move it under a studio: section in pack.yaml (run harnest upgrade on an agent for details)", path)
		}
		info, err := os.Stat(filepath.Join(absolute, packManifestFile))
		if err != nil || !info.Mode().IsRegular() {
			return nil, fmt.Errorf("pack must contain pack.yaml: %s", path)
		}
		folders = append(folders, absolute)
	}
	return folders, nil
}

// packPlanArguments builds the Python pack action shared by init and upgrade.
func packPlanArguments(action, directory, executable string, packs, options []string) []string {
	arguments := []string{"pack", action, directory, "--harnest", executable}
	for _, pack := range packs {
		arguments = append(arguments, "--pack", pack)
	}
	for _, option := range options {
		arguments = append(arguments, "--option", option)
	}
	return arguments
}

// runPackPython runs one `python -m harnest.cli pack ...` action.
func (a *application) runPackPython(command *cobra.Command, arguments []string) error {
	python, err := a.resolvePython()
	if err != nil {
		return err
	}
	err = runPythonCLI(
		command.Context(), a, python, arguments, nil,
		command.InOrStdin(), command.OutOrStdout(), command.ErrOrStderr(),
	)
	var exit *exec.ExitError
	if errors.As(err, &exit) && exit.ExitCode() > 0 {
		return exitCodeError{code: exit.ExitCode()}
	}
	return err
}

func (a *application) newPackValidateCommand() *cobra.Command {
	return &cobra.Command{
		Use: "validate PACK...", Short: "Check pack.yaml, pack.py hooks and the studio sub-pack", Args: cobra.MinimumNArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			packs, err := resolvePackFolders(arguments)
			if err != nil {
				return err
			}
			return a.runPackPython(command, append([]string{"pack", "validate"}, packs...))
		},
	}
}

func (a *application) newPackBuildCommand() *cobra.Command {
	var output string
	command := &cobra.Command{
		Use:   "build PACK",
		Short: "Build a wheel that installs the pack's branded command",
		Long: `Build harnest-pack-NAME as a wheel. Installing it adds the pack's command
(pack.yaml command, default NAME): COMMAND init|upgrade|studio run Harnest with
this pack attached, the pack's own commands run its handlers, and anything else
is passed to Harnest. The command calls the harnest executable on PATH, or
HARNEST_CLI when set.`,
		Args: cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			packs, err := resolvePackFolders(arguments)
			if err != nil {
				return err
			}
			absolute, err := filepath.Abs(output)
			if err != nil {
				return err
			}
			return a.runPackPython(command, []string{"pack", "build", packs[0], "--output", absolute})
		},
	}
	command.Flags().StringVarP(&output, "output", "o", "dist", "output directory")
	return command
}

func (a *application) newPackRunCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "run PACK COMMAND [ARGS...]",
		Short: "Run one of the pack's own commands",
		// Company commands own their flags; pass them through untouched.
		DisableFlagParsing: true,
		RunE: func(command *cobra.Command, arguments []string) error {
			if len(arguments) < 2 || arguments[0] == "-h" || arguments[0] == "--help" {
				return command.Help()
			}
			packs, err := resolvePackFolders(arguments[:1])
			if err != nil {
				return err
			}
			return a.runPackPython(command, append([]string{"pack", "run", packs[0]}, arguments[1:]...))
		},
	}
}

// newPackTypesCommand exports the Studio UI declarations used by studio sub-pack UI code.
func (a *application) newPackTypesCommand() *cobra.Command {
	var output string
	command := &cobra.Command{Use: "types", Short: "Export the Studio UI TypeScript package for studio sub-packs", Args: cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			return a.runStudioPack(command, []string{"types", "--output", output})
		},
	}
	command.Flags().StringVar(&output, "output", "dist", "Output directory")
	return command
}
