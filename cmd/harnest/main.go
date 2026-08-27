package main

import (
	"context"
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
		fmt.Fprintln(os.Stderr, "harnest:", err)
		os.Exit(1)
	}
}
