package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestProvisionDisabled rejects every operation before resolving or launching Python.
func TestProvisionDisabled(t *testing.T) {
	t.Setenv("HARNEST_ENABLE_DEPLOYMENT", "")
	marker := filepath.Join(t.TempDir(), "called")
	t.Setenv("HARNEST_TEST_RECORD", marker)
	python := writeExecutable(t, "#!/bin/sh\ntouch \"$HARNEST_TEST_RECORD\"\n")
	for _, operation := range []string{"init", "plan", "apply", "status", "stop", "remove", "history", "rollback"} {
		_, _, err := executeForTest(t, defaultSystem(), "--python", python, "provision", operation)
		if err == nil || !strings.Contains(err.Error(), "HARNEST_ENABLE_DEPLOYMENT=true") {
			t.Fatalf("%s: expected disabled error, got %v", operation, err)
		}
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatalf("disabled provision invoked Python: %v", err)
	}
}

// TestProvisionHelpVisibility excludes the disabled feature from ordinary CLI discovery.
func TestProvisionHelpVisibility(t *testing.T) {
	for _, value := range []string{"", "true"} {
		t.Setenv("HARNEST_ENABLE_DEPLOYMENT", value)
		stdout, _, err := executeForTest(t, defaultSystem(), "--help")
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(stdout, "  provision ") != (value == "true") {
			t.Fatalf("incorrect provision visibility with flag %q", value)
		}
	}
}

// TestProvisionDelegatesWithoutCompiling lets service-only projects deploy through the shared engine.
func TestProvisionDelegatesWithoutCompiling(t *testing.T) {
	t.Setenv("HARNEST_ENABLE_DEPLOYMENT", "true")
	record := filepath.Join(t.TempDir(), "arguments.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	python := writeExecutable(t, `#!/bin/sh
printf '%s\n' "$@" > "$HARNEST_TEST_RECORD"
`)
	for _, operation := range []string{"init", "plan", "apply", "status", "stop", "remove"} {
		_, _, err := executeForTest(t, defaultSystem(), "--python", python, "provision", operation, "--project", "folder with spaces", "--environment", "production")
		if err != nil {
			t.Fatal(err)
		}
		assertContainsAll(t, "provisioner arguments", string(mustReadTestFile(t, record)), []string{
			"-m\nharnest.provisioner_cli\n" + operation, "--project\nfolder with spaces", "--environment\nproduction",
		})
	}
}

// TestProvisionRevisionArguments preserves revision selection and history pagination across the process boundary.
func TestProvisionRevisionArguments(t *testing.T) {
	t.Setenv("HARNEST_ENABLE_DEPLOYMENT", "true")
	record := filepath.Join(t.TempDir(), "arguments.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	python := writeExecutable(t, `#!/bin/sh
printf '%s\n' "$@" > "$HARNEST_TEST_RECORD"
`)
	_, _, err := executeForTest(t, defaultSystem(), "--python", python, "provision", "rollback", "--revision", "12")
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "rollback arguments", string(mustReadTestFile(t, record)), []string{"rollback", "--revision\n12"})
	_, _, err = executeForTest(t, defaultSystem(), "--python", python, "provision", "history", "--limit", "5", "--before-revision", "12")
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "history arguments", string(mustReadTestFile(t, record)), []string{"history", "--limit\n5", "--before-revision\n12"})
}
