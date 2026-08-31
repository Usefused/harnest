package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestServeReloadRejectsProductionAndRetainedArtifactOptions(t *testing.T) {
	testCases := []struct {
		name    string
		options serveOptions
		want    string
	}{
		{
			name: "retained output",
			options: serveOptions{
				reload: true, host: "127.0.0.1", output: "/tmp/artifact",
			},
			want: "cannot use --output",
		},
		{
			name: "remote opt-in",
			options: serveOptions{
				reload: true, host: "127.0.0.1", allowRemote: true,
			},
			want: "development-only",
		},
		{
			name: "remote host",
			options: serveOptions{
				reload: true, host: "0.0.0.0",
			},
			want: "loopback",
		},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			err := testCase.options.validateReload()
			if err == nil || !strings.Contains(err.Error(), testCase.want) {
				t.Fatalf("got error %v, want %q", err, testCase.want)
			}
		})
	}
	for _, host := range []string{"127.0.0.1", "::1", "localhost", "LOCALHOST"} {
		if err := (serveOptions{reload: true, host: host}).validateReload(); err != nil {
			t.Fatalf("loopback host %q rejected: %v", host, err)
		}
	}
}

func TestReloadWatchStateDebouncesAndResetsCandidates(t *testing.T) {
	start := time.Unix(1, 0)
	state := reloadWatchState{baseline: "first"}
	if state.ready("second", start) {
		t.Fatal("new digest bypassed debounce")
	}
	if state.ready("third", start.Add(reloadDebounce)) {
		t.Fatal("changed candidate reused the previous debounce window")
	}
	if state.ready("third", start.Add(reloadDebounce+time.Millisecond)) {
		t.Fatal("candidate stabilized for less than the debounce period")
	}
	if !state.ready("third", start.Add(2*reloadDebounce+time.Millisecond)) {
		t.Fatal("stable candidate did not become ready")
	}
	state.reset("third")
	if state.ready("third", start.Add(3*reloadDebounce)) {
		t.Fatal("baseline digest triggered another reload")
	}
}

func TestServeReloadKeepsLastGoodGenerationAndRestartsOnRecovery(t *testing.T) {
	target, _, python := serveRecordingFixture(t)
	record := filepath.Join(t.TempDir(), "reload-record.txt")
	t.Setenv("HARNEST_TEST_RELOAD_AGENT", target)
	t.Setenv("HARNEST_TEST_RELOAD_RECORD", record)
	python = writeExecutable(t, reloadTestPython)

	ctx, cancel := context.WithCancel(context.Background())
	done := startReloadCommand(t, ctx, python, target)
	waitForReloadRecord(t, record, func(value string) bool {
		return strings.Count(value, "START\t") == 1
	})

	instructions := filepath.Join(target, "instructions.md")
	if err := os.WriteFile(instructions, []byte("BROKEN reload\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitForReloadRecord(t, record, func(value string) bool {
		return strings.Contains(value, "COMPILE_FAIL")
	})
	failed := string(mustReadTestFile(t, record))
	if strings.Contains(failed, "STOP\t") || strings.Count(failed, "START\t") != 1 {
		t.Fatalf("failed compilation replaced the active generation:\n%s", failed)
	}

	if err := os.WriteFile(instructions, []byte("Recovered reload\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitForReloadRecord(t, record, func(value string) bool {
		return strings.Count(value, "START\t") == 2 &&
			strings.Contains(value, "Recovered reload")
	})
	assertDistinctReloadGenerations(t, string(mustReadTestFile(t, record)))

	pyproject := filepath.Join(target, "pyproject.toml")
	contents := append(mustReadTestFile(t, pyproject), []byte("\n# reload dependency input\n")...)
	if err := os.WriteFile(pyproject, contents, 0o644); err != nil {
		t.Fatal(err)
	}
	waitForReloadRecord(t, record, func(value string) bool {
		return strings.Count(value, "START\t") == 3
	})

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("reload command shutdown failed: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("reload command did not stop after cancellation")
	}
	waitForReloadRecord(t, record, func(value string) bool {
		return strings.Count(value, "STOP\t") == 3
	})
}

// startReloadCommand executes Cobra with file-backed output safe for concurrent reads.
func startReloadCommand(
	t *testing.T, ctx context.Context, python, target string,
) <-chan error {
	t.Helper()
	command := newRootCommand(defaultSystem(), "test-version")
	stdout, err := os.Create(filepath.Join(t.TempDir(), "reload-stdout.txt"))
	if err != nil {
		t.Fatal(err)
	}
	stderr, err := os.Create(filepath.Join(t.TempDir(), "reload-stderr.txt"))
	if err != nil {
		t.Fatal(err)
	}
	command.SetOut(stdout)
	command.SetErr(stderr)
	command.SetIn(strings.NewReader(""))
	command.SetArgs([]string{"--python", python, "serve", target, "--reload"})
	done := make(chan error, 1)
	go func() {
		done <- command.ExecuteContext(ctx)
		_ = stdout.Close()
		_ = stderr.Close()
	}()
	return done
}

// waitForReloadRecord bounds asynchronous supervisor assertions without fixed sleeps.
func waitForReloadRecord(
	t *testing.T, path string, ready func(string) bool,
) {
	t.Helper()
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		contents, err := os.ReadFile(path)
		if err == nil && ready(string(contents)) {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	contents, _ := os.ReadFile(path)
	t.Fatalf("reload record did not reach expected state:\n%s", contents)
}

// assertDistinctReloadGenerations proves processes never reuse a mutable artifact.
func assertDistinctReloadGenerations(t *testing.T, record string) {
	t.Helper()
	seen := map[string]struct{}{}
	for _, line := range strings.Split(record, "\n") {
		fields := strings.Split(line, "\t")
		if len(fields) >= 2 && fields[0] == "START" {
			seen[fields[1]] = struct{}{}
		}
	}
	if len(seen) != 2 {
		t.Fatalf("reload reused an artifact generation:\n%s", record)
	}
}

const reloadTestPython = `#!/bin/sh
record="$HARNEST_TEST_RELOAD_RECORD"
if [ "$1" = "-m" ]; then
  output=""
  previous=""
  for value in "$@"; do
    if [ "$previous" = "--output" ]; then output="$value"; fi
    previous="$value"
  done
  if grep -q BROKEN "$HARNEST_TEST_RELOAD_AGENT/instructions.md"; then
    printf 'COMPILE_FAIL\n' >> "$record"
    exit 9
  fi
  mkdir -p "$output/source"
  cp "$HARNEST_TEST_RELOAD_AGENT/instructions.md" "$output/source/instructions.md"
  printf 'generated launcher\n' > "$output/harnest-agent"
  printf 'COMPILE\t%s\n' "$output" >> "$record"
  exit 0
fi
artifact=$(dirname "$1")
instruction=$(tr -d '\n' < "$artifact/source/instructions.md")
printf 'START\t%s\t%s\n' "$artifact" "$instruction" >> "$record"
trap 'printf "STOP\t%s\n" "$artifact" >> "$record"; exit 0' INT TERM
while true; do sleep 0.1; done
`
