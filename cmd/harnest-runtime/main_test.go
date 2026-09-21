package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestRunDisabled rejects orchestration before reading source or creating artifacts.
func TestRunDisabled(t *testing.T) {
	t.Setenv("HARNEST_ENABLE_DEPLOYMENT", "")
	output := filepath.Join(t.TempDir(), "compiled")
	var stderr bytes.Buffer
	code := run([]string{"-orchestrator", "/missing/orchestrator.py", "-compiled-root", output}, strings.NewReader(""), &bytes.Buffer{}, &stderr)
	if code != 1 || !strings.Contains(stderr.String(), "HARNEST_ENABLE_DEPLOYMENT=true") {
		t.Fatalf("got code %d and stderr %q", code, stderr.String())
	}
	if _, err := os.Stat(output); !os.IsNotExist(err) {
		t.Fatalf("disabled runtime created artifacts: %v", err)
	}
}

func TestRunRejectsUnexpectedPositionalArguments(t *testing.T) {
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	code := run([]string{"-plan", "-", "extra"}, strings.NewReader(""), &stdout, &stderr)
	if code != 2 {
		t.Fatalf("got exit code %d, want 2", code)
	}
	if !strings.Contains(stderr.String(), "unexpected positional arguments") {
		t.Fatalf("unexpected stderr %q", stderr.String())
	}
}

func TestRunReportsPlanOpenContext(t *testing.T) {
	t.Setenv("HARNEST_ENABLE_DEPLOYMENT", "true")
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	code := run([]string{"-plan", "/path/that/does/not/exist"}, strings.NewReader(""), &stdout, &stderr)
	if code != 2 {
		t.Fatalf("got exit code %d, want 2", code)
	}
	if !strings.Contains(stderr.String(), "open deployment plan") {
		t.Fatalf("unexpected stderr %q", stderr.String())
	}
}

func TestPlanReaderRejectsEmptyPythonExecutable(t *testing.T) {
	_, _, err := planReader("orchestrator.py", "", " ", strings.NewReader(""), &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "-python cannot be empty") {
		t.Fatalf("got error %v", err)
	}
}
