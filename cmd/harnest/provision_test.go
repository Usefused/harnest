package main

import (
	"path/filepath"
	"testing"
)

// TestProvisionDelegatesWithoutCompiling lets service-only projects deploy through the shared engine.
func TestProvisionDelegatesWithoutCompiling(t *testing.T) {
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
