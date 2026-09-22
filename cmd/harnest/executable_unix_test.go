//go:build !windows

package main

import (
	"bufio"
	"context"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/spf13/cobra"
	"harnest.dev/harnest/internal/agentpack"
)

// TestNativeExecutableForwardsTermination exercises graceful shutdown through the real native boundary.
func TestNativeExecutableForwardsTermination(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	command := &cobra.Command{}
	command.SetContext(ctx)
	launcher, err := loadAgentLauncher(command)
	if err != nil {
		t.Fatal(err)
	}
	script := "#!/bin/sh\ntrap 'exit 17' TERM\nprintf 'ready\\n'\nwhile :; do /bin/sleep 1; done\n"
	pack, artifact, m := executableFixture(t, script)
	output := filepath.Join(t.TempDir(), "agent")
	if err := agentpack.WriteExecutable(output, launcher, artifact, pack, m, false, nil); err != nil {
		t.Fatal(err)
	}
	if err := signAgentExecutable(command, &application{system: defaultSystem()}, output); err != nil {
		t.Fatal(err)
	}
	child := exec.CommandContext(ctx, output, "--runtime", pack, "serve")
	child.Env = []string{"PATH=/nonexistent", "HARNEST_AGENT_CACHE=" + t.TempDir()}
	assertNativeTermination(t, child)
}

// assertNativeTermination waits for readiness before sending a signal, avoiding startup races.
func assertNativeTermination(t *testing.T, child *exec.Cmd) {
	t.Helper()
	stdout, err := child.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err = child.Start(); err != nil {
		t.Fatal(err)
	}
	defer child.Process.Kill()
	ready, err := bufio.NewReader(stdout).ReadString('\n')
	if err != nil || ready != "ready\n" {
		t.Fatalf("readiness: %q, %v", ready, err)
	}
	if err = child.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	err = child.Wait()
	exit, ok := err.(*exec.ExitError)
	if !ok || exit.ExitCode() != 17 {
		t.Fatalf("termination status: %v", err)
	}
}
