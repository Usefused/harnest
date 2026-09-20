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
	var port int
	command := &cobra.Command{
		Use: "studio", Short: "Start Harnest Studio in the current folder or a selected workspace", Args: cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			directory, err := studioWorkspace(workspace, port)
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
