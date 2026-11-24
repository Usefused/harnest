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
	python, err := a.packPython(command)
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

// newPackBuildCommand builds either a standalone branded CLI binary (default) or a launcher wheel.
func (a *application) newPackBuildCommand() *cobra.Command {
	var output, format, baseBinary string
	command := &cobra.Command{
		Use:   "build PACK",
		Short: "Build a standalone branded CLI binary (or wheel) for this pack",
		Long: `Build a standalone branded CLI binary (default, --format binary) that embeds
Harnest plus this pack's hooks, Studio sub-pack, coding-IDE skills, protected
Python packages, and auto-loaded Harnest Extensions so downstream teams do not
need to install Harnest separately.

Pass --format wheel to build the lightweight harnest-pack-NAME Python launcher
wheel that delegates to an existing harnest executable on PATH.`,
		Args: cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			return a.runPackBuild(command, arguments[0], output, format, baseBinary)
		},
	}
	command.Flags().StringVarP(&output, "output", "o", "dist", "output directory")
	command.Flags().StringVar(&format, "format", "binary", "output format: binary (default) or wheel")
	command.Flags().StringVar(&baseBinary, "base-binary", "", "with --format binary: path to a base harnest executable to stamp (default: current binary)")
	return command
}

// runPackBuild validates format flags and dispatches to binary stamping or wheel building.
func (a *application) runPackBuild(
	command *cobra.Command, packArg, output, format, baseBinary string,
) error {
	if format != "binary" && format != "wheel" {
		return fmt.Errorf("--format must be binary or wheel")
	}
	if format == "wheel" && baseBinary != "" {
		return fmt.Errorf("--base-binary requires --format binary")
	}
	packs, err := resolvePackFolders([]string{packArg})
	if err != nil {
		return err
	}
	absolute, err := filepath.Abs(output)
	if err != nil {
		return err
	}
	if format == "wheel" {
		return a.runPackPython(command, []string{"pack", "build", packs[0], "--output", absolute})
	}
	return a.buildPackBinary(command, packs[0], absolute, baseBinary)
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
