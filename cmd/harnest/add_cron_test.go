package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestAddCronRejectsInvalidSchedulesBeforeWriting preserves source on invalid input.
func TestAddCronRejectsInvalidSchedulesBeforeWriting(t *testing.T) {
	root := filepath.Join(t.TempDir(), "cron-agent")
	if _, _, err := executeForTest(t, defaultSystem(), "init", root, "--minimal"); err != nil {
		t.Fatal(err)
	}
	for _, flags := range [][]string{
		{"--schedule", "60 9 * * *"}, {"--schedule", "0 9 * *"},
		{"--schedule", "0 9 * * MON"}, {"--schedule", "0 9 * * *", "--dynamic"},
	} {
		args := append([]string{"add", "cron", "daily-report", "--project", root}, flags...)
		if _, _, err := executeForTest(t, defaultSystem(), args...); err == nil {
			t.Fatalf("accepted %v", flags)
		}
	}
	if _, err := os.Stat(filepath.Join(root, "cron")); !os.IsNotExist(err) {
		t.Fatalf("invalid input created cron directory: %v", err)
	}
	if _, _, err := executeForTest(t, defaultSystem(), "add", "cron", "daily-report", "--project", root, "--dynamic"); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "cron", "daily_report.py")
	before := string(mustReadTestFile(t, path))
	if _, _, err := executeForTest(t, defaultSystem(), "add", "cron", "daily-report", "--project", root); err == nil || !strings.Contains(err.Error(), "already exists") {
		t.Fatalf("expected duplicate refusal, got %v", err)
	}
	if after := string(mustReadTestFile(t, path)); after != before {
		t.Fatal("duplicate overwrote authored source")
	}
}
