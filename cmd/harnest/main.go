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

// main boots the CLI, loads any embedded pack payload, and formats top-level errors with the active command name.
func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	sys := defaultSystem()
	embedded, err := loadEmbeddedPack(sys)
	if err != nil {
		// Fall back to HARNEST_CLI_NAME (when valid) or "harnest" because pack metadata could not be loaded yet.
		prefix := sanitizeCLIName(sys.getenv(cliNameEnv))
		if prefix == "" {
			prefix = "harnest"
		}
		fmt.Fprintf(os.Stderr, "%s: %v\n", prefix, err)
		os.Exit(1)
	}
	command := newRootCommandWithPack(sys, version, embedded)
	command.SetArgs(os.Args[1:])
	if err := command.ExecuteContext(ctx); err != nil {
		var exit exitCodeError
		if errors.As(err, &exit) {
			// The child process already reported its own failure.
			os.Exit(exit.code)
		}
		fmt.Fprintf(os.Stderr, "%s: %v\n", command.Use, err)
		os.Exit(1)
	}
}

// exitCodeError carries a child process's exit code to main without adding
// a second error message, so pack commands and blocked plans keep their codes.
type exitCodeError struct{ code int }

func (e exitCodeError) Error() string { return fmt.Sprintf("exit status %d", e.code) }
