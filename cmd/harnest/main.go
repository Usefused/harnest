// Entry point of the `harnest` CLI.

package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"syscall"
)

var version = "dev"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	command := newRootCommand(defaultSystem(), version)
	command.SetArgs(os.Args[1:])
	if err := command.ExecuteContext(ctx); err != nil {
		var exit exitCodeError
		if errors.As(err, &exit) {
			// The child process already reported its own failure.
			os.Exit(exit.code)
		}
		fmt.Fprintln(os.Stderr, "harnest:", err)
		os.Exit(1)
	}
}

// exitCodeError carries a child process's exit code to main without adding
// a second error message, so pack commands and blocked plans keep their codes.
type exitCodeError struct{ code int }

func (e exitCodeError) Error() string { return fmt.Sprintf("exit status %d", e.code) }
