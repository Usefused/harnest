//go:build !windows

package main

import (
	"errors"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
)

// waitAgent forwards shutdown to the whole agent process group and preserves exit status.
func waitAgent(command *exec.Cmd) (int, error) {
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)
	if err := command.Start(); err != nil {
		return 1, err
	}
	done := make(chan error, 1)
	go func() { done <- command.Wait() }()
	for {
		select {
		case sig := <-signals:
			_ = syscall.Kill(-command.Process.Pid, sig.(syscall.Signal))
		case err := <-done:
			var exit *exec.ExitError
			if errors.As(err, &exit) {
				status := exit.Sys().(syscall.WaitStatus)
				if status.Signaled() {
					return 128 + int(status.Signal()), nil
				}
				return exit.ExitCode(), nil
			}
			if err != nil {
				return 1, err
			}
			return 0, nil
		}
	}
}
