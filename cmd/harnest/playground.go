package main

import (
	"fmt"
	"os"
	"path/filepath"

	playground "harnest.dev/harnest/src/harnest/_playground"
)

// stagePlayground owns development-only assets outside every compiled artifact.
func stagePlayground() (string, func(), error) {
	directory, err := os.MkdirTemp("", "harnest-playground-")
	if err != nil {
		return "", nil, fmt.Errorf("create playground directory: %w", err)
	}
	cleanup := func() { _ = os.RemoveAll(directory) }
	if err := writePlayground(directory); err != nil {
		cleanup()
		return "", nil, err
	}
	return directory, cleanup, nil
}

// writePlayground copies the fixed embedded asset set without accepting user paths.
func writePlayground(directory string) error {
	entries, err := playground.Files.ReadDir(".")
	if err != nil {
		return fmt.Errorf("read embedded playground: %w", err)
	}
	for _, entry := range entries {
		contents, err := playground.Files.ReadFile(entry.Name())
		if err != nil {
			return fmt.Errorf("read playground asset: %w", err)
		}
		if err := os.WriteFile(filepath.Join(directory, entry.Name()), contents, 0600); err != nil {
			return fmt.Errorf("stage playground asset: %w", err)
		}
	}
	return nil
}
