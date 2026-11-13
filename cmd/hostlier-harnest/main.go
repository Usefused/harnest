// Dispatches Hostlier package discovery, initialization, and build hooks.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
)

// run serves the optional local project hooks and Hostlier's public package discovery operation.
func run(ctx context.Context, args []string, input io.Reader, output io.Writer) error {
	if len(args) != 1 {
		return errors.New("expected one Hostlier package operation")
	}
	switch args[0] {
	case "describe":
		return json.NewEncoder(output).Encode(map[string]any{
			"version": 1, "name": "harnest", "hooks": []string{"init", "project-plan", "project-package"},
			"configSchema": settingsSchema(), "outputSchema": map[string]any{}, "outputs": []string{},
		})
	case "init":
		return dispatchInit(input, output)
	case "project-plan", "project-package":
		return dispatchHook(ctx, args[0], input, output)
	default:
		return errors.New("unsupported Harnest package operation")
	}
}

// main reserves stdout for public machine-readable package output.
func main() {
	if err := run(context.Background(), os.Args[1:], os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
