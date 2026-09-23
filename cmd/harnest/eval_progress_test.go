package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"
)

func TestEvalProgressPreservesOutputAndPartialLines(t *testing.T) {
	var stdout, stderr bytes.Buffer
	p := &evalProgress{stderr: &stderr, terminal: true, started: time.Now(), stage: "Evaluating"}
	p.tick(p.started.Add(time.Second))
	if !strings.Contains(stderr.String(), "1s elapsed") {
		t.Fatalf("spinner missing: %q", stderr.String())
	}
	w := evalProgressWriter{p, &stdout, 0}
	_, _ = w.Write([]byte(`{"result":`))
	before := stderr.String()
	p.tick(p.started.Add(2 * time.Second))
	if stderr.String() != before {
		t.Fatal("spinner overwrote an unfinished subprocess line")
	}
	_, _ = w.Write([]byte("true}\n"))
	p.tick(p.started.Add(3 * time.Second))
	if stdout.String() != "{\"result\":true}\n" || !strings.Contains(stderr.String(), "3s elapsed") {
		t.Fatalf("output changed or spinner did not resume: %q, %q", stdout.String(), stderr.String())
	}
	_, _ = (evalProgressWriter{p, &stderr, 1}).Write([]byte("provider warning\n"))
	if !strings.HasSuffix(stderr.String(), "\r\x1b[2Kprovider warning\n") {
		t.Fatalf("warning collided with spinner: %q", stderr.String())
	}
}

func TestEvalProgressPlainHeartbeatAndCleanup(t *testing.T) {
	var stdout, stderr bytes.Buffer
	command := &cobra.Command{}
	command.SetOut(&stdout)
	command.SetErr(&stderr)
	p := startEvalProgress(command, true, "dumb")
	p.tick(p.started.Add(16 * time.Second))
	p.phase("Running Python tests and evaluations")
	p.close()
	if strings.ContainsAny(stderr.String(), "\x1b\r") || !strings.Contains(stderr.String(), "still running (16s elapsed)") {
		t.Fatalf("invalid plain progress: %q", stderr.String())
	}
	if command.OutOrStdout() != &stdout || command.ErrOrStderr() != &stderr {
		t.Fatal("command output was not restored")
	}
	select {
	case <-p.done:
	default:
		t.Fatal("renderer is still running after cleanup")
	}
	before := stderr.String()
	quiet := startEvalProgress(command, false, "xterm")
	quiet.phase("must not appear")
	quiet.close()
	if stderr.String() != before {
		t.Fatal("quiet mode emitted progress")
	}
}

type failedEvalWriter struct{}

func (failedEvalWriter) Write([]byte) (int, error) { return 0, io.ErrClosedPipe }

func TestEvalProgressPreservesWriteErrors(t *testing.T) {
	p := &evalProgress{stderr: io.Discard}
	n, err := (evalProgressWriter{p, failedEvalWriter{}, 0}).Write([]byte("test"))
	if n != 0 || !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("write error lost: %d, %v", n, err)
	}
}

func TestEvalCommandProgressPreservesFailureAndQuietMode(t *testing.T) {
	target := filepath.Join(t.TempDir(), "eval-agent")
	if err := createScaffold(target, "eval-agent"); err != nil {
		t.Fatal(err)
	}
	python := writeExecutable(t, "#!/bin/sh\nprintf 'result bytes\\n'\nprintf 'provider error\\n' >&2\nexit 7\n")
	stdout, stderr, err := executeForTest(t, defaultSystem(), "--python", python, "test", target, "--evals")
	if err == nil || !strings.Contains(err.Error(), "exit status 7") || stdout != "result bytes\n" {
		t.Fatalf("subprocess result changed: %q, %q, %v", stdout, stderr, err)
	}
	assertContainsAll(t, "eval progress", stderr, []string{
		"Preparing evaluation environment", "Running Python tests and evaluations", "provider error",
	})
	_, stderr, _ = executeForTest(t, defaultSystem(), "--python", python, "test", target, "--evals", "--no-output")
	if strings.Contains(stderr, "harnest eval:") {
		t.Fatalf("quiet mode emitted progress: %q", stderr)
	}
}

func TestEvalPythonOutputIsUnbuffered(t *testing.T) {
	python := writeExecutable(t, "#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
	app := &application{system: defaultSystem()}
	var stdout bytes.Buffer
	err := runPythonCLI(context.Background(), app, pythonSelection{Executable: python}, []string{"test", "."}, nil, nil, &stdout, io.Discard)
	if err != nil || !strings.HasPrefix(stdout.String(), "-u\n-m\nharnest.cli\ntest\n") {
		t.Fatalf("test subprocess can buffer progress: %q, %v", stdout.String(), err)
	}
}
