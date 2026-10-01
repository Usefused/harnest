package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"time"

	"github.com/spf13/cobra"
)

// newStudioCommand starts the bundled local server against the caller's workspace.
func (a *application) newStudioCommand() *cobra.Command {
	var workspace string
	var packs []string
	var port int
	command := &cobra.Command{
		Use: "studio", Short: "Start Harnest Studio in the current folder or a selected workspace", Args: cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			directory, err := studioWorkspace(workspace, port)
			if err != nil {
				return err
			}
			packArguments, err := studioPackArguments(packs)
			if err != nil {
				return err
			}
			python, err := a.studioPython(command)
			if err != nil {
				return err
			}
			executable, err := os.Executable()
			if err != nil {
				return fmt.Errorf("resolve Harnest executable for Studio: %w", err)
			}
			arguments := []string{"-m", "harnest_builder", "--workspace", directory, "--port", strconv.Itoa(port), "--cli", executable}
			arguments = append(arguments, packArguments...)
			if python.Source == "Studio environment" {
				arguments = append([]string{"-I"}, arguments...)
			}
			server := a.system.commandContext(command.Context(), python.Executable, arguments...)
			// Let Uvicorn run lifespan cleanup before the process deadline so
			// supervised commands and the private assistant server are reaped.
			server.Cancel = func() error {
				if runtime.GOOS == "windows" {
					return exec.Command("taskkill", "/PID", strconv.Itoa(server.Process.Pid), "/T", "/F").Run()
				}
				return server.Process.Signal(os.Interrupt)
			}
			server.WaitDelay = 15 * time.Second
			// Provisioning uses this interpreter; agent builds retain their locked runtimes.
			overrides := map[string]string{"HARNEST_PYTHON": python.Executable, "PYTHONDONTWRITEBYTECODE": "1"}
			if python.Source == "Studio environment" {
				overrides["PYTHONPATH"] = ""
				overrides["PYTHONHOME"] = ""
			}
			server.Env = mergedEnvironment(overrides)
			if err := runCommand(server, command.InOrStdin(), command.OutOrStdout(), command.ErrOrStderr()); err != nil {
				return fmt.Errorf("run Harnest Studio: %w", err)
			}
			return nil
		},
	}
	command.Flags().StringVar(&workspace, "workspace", ".", "existing workspace folder (default: current working directory)")
	command.Flags().IntVar(&port, "port", 1940, "local Studio port (1024-65535)")
	command.Flags().StringArrayVar(&packs, "pack", nil, "local Studio Pack folder (repeatable)")
	command.AddCommand(a.newStudioPackCommand())
	return command
}

// studioWorkspace validates user input before creating or installing a managed environment.
func studioWorkspace(workspace string, port int) (string, error) {
	if port < 1024 || port > 65535 {
		return "", fmt.Errorf("--port must be between 1024 and 65535")
	}
	directory, err := filepath.Abs(workspace)
	if err != nil {
		return "", fmt.Errorf("resolve Studio workspace: %w", err)
	}
	info, err := os.Stat(directory)
	if err != nil || !info.IsDir() {
		return "", fmt.Errorf("--workspace must be an existing directory: %s", directory)
	}
	return directory, nil
}

// studioPackArguments resolves company packs before bootstrapping the Studio environment.
func studioPackArguments(packs []string) ([]string, error) {
	var arguments []string
	for _, path := range packs {
		absolute, err := filepath.Abs(path)
		if err != nil {
			return nil, err
		}
		info, err := os.Stat(filepath.Join(absolute, "studio-pack.yaml"))
		if err != nil || !info.Mode().IsRegular() {
			return nil, fmt.Errorf("Studio pack must contain studio-pack.yaml: %s", path)
		}
		arguments = append(arguments, "--pack", absolute)
	}
	return arguments, nil
}

// newStudioPackCommand keeps inherited CLI options separate from pack arguments.
func (a *application) newStudioPackCommand() *cobra.Command {
	command := &cobra.Command{Use: "pack", Short: "Validate or package company Studio Packs"}
	validate := &cobra.Command{Use: "validate PACK...", Short: "Validate local Studio Packs", Args: cobra.MinimumNArgs(1),
		RunE: func(command *cobra.Command, args []string) error {
			return a.runStudioPack(command, append([]string{"validate"}, args...))
		},
	}
	command.AddCommand(validate, a.newStudioPackPackageCommand())
	return command
}

// newStudioPackPackageCommand exposes only the options supported by the distribution contract.
func (a *application) newStudioPackPackageCommand() *cobra.Command {
	var packs []string
	var name, version, output string
	command := &cobra.Command{Use: "package", Short: "Build a company launcher wheel with embedded packs", Args: cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			arguments := []string{"package", "--name", name, "--version", version, "--output", output}
			for _, pack := range packs {
				arguments = append(arguments, "--pack", pack)
			}
			return a.runStudioPack(command, arguments)
		},
	}
	command.Flags().StringArrayVar(&packs, "pack", nil, "Studio Pack folder (repeatable)")
	command.Flags().StringVar(&name, "name", "", "Company distribution name")
	command.Flags().StringVar(&version, "version", "", "Company distribution version")
	command.Flags().StringVar(&output, "output", "dist", "Output directory")
	for _, flag := range []string{"pack", "name", "version"} {
		_ = command.MarkFlagRequired(flag)
	}
	return command
}

// runStudioPack uses the same installed Studio runtime as the local server.
func (a *application) runStudioPack(command *cobra.Command, args []string) error {
	python, err := a.studioPython(command)
	if err != nil {
		return err
	}
	arguments := append([]string{"-m", "harnest_builder", "pack"}, args...)
	if python.Source == "Studio environment" {
		arguments = append([]string{"-I"}, arguments...)
	}
	process := a.system.commandContext(command.Context(), python.Executable, arguments...)
	return runCommand(process, command.InOrStdin(), command.OutOrStdout(), command.ErrOrStderr())
}
