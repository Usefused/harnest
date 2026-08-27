package main

import (
	"bytes"
	"strings"
	"testing"
)

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
