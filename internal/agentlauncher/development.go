//go:build !harnest_release

package agentlauncher

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
)

var embeddedLauncher []byte

// Load compiles the tiny launcher only for contributor builds without release assets.
func Load(ctx context.Context) ([]byte, error) {
	_, source, _, ok := runtime.Caller(0)
	if !ok {
		return Embedded()
	}
	temporary, err := os.MkdirTemp("", "harnest-launcher-*")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(temporary)
	output := filepath.Join(temporary, "launcher")
	command := exec.CommandContext(ctx, "go", "build", "-trimpath", "-ldflags=-s -w", "-o", output, "./cmd/harnest-agent")
	command.Dir = filepath.Dir(filepath.Dir(filepath.Dir(source)))
	if data, err := command.CombinedOutput(); err != nil {
		return nil, fmt.Errorf("build source launcher: %w: %s", err, data)
	}
	return os.ReadFile(output)
}
