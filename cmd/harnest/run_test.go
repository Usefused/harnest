package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRunCompilesAndInvokesGeneratedLauncherLocally(t *testing.T) {
	target, record, python := runRecordingFixture(t)
	stdout, stderr, err := executeRunForTest(
		t,
		strings.NewReader(""),
		"--python", python, "run", target, "private prompt",
		"--session", "session-123", "--output", "ndjson",
	)
	if err != nil {
		t.Fatal(err)
	}
	assertRunStdout(t, stdout)
	assertContainsAll(t, "run stderr", stderr, []string{
		"compiler-diagnostic", "agent-diagnostic",
	})
	lines := recordedRunLines(t, record)
	assertContainsAll(t, "compile call", lines[0], []string{
		"CALL\t-m\tharnest.cli\tcompile", "\t--output\t", target, "\t--enable-cli",
	})
	launcher := assertRunCall(t, lines[1], "run\t--session\tsession-123\t--output\tndjson")
	if strings.Contains(lines[1], "private prompt") {
		t.Fatalf("run argv exposed the prompt: %q", lines[1])
	}
	if lines[2] != "INPUT\tprivate prompt" {
		t.Fatalf("launcher stdin = %q", lines[2])
	}
	if lines[3] != "ENV\tgpt-4.1-mini\thttps://api.openai.com/v1" {
		t.Fatalf("launcher environment = %q", lines[3])
	}
	assertEphemeralArtifactRemoved(t, launcher)
}

func TestRunAcceptsMessageFromStdin(t *testing.T) {
	target, record, python := runRecordingFixture(t)
	stdout, _, err := executeRunForTest(
		t, strings.NewReader("piped prompt\n"),
		"--python", python, "run", target,
	)
	if err != nil {
		t.Fatal(err)
	}
	assertRunStdout(t, stdout)
	contents := string(mustReadTestFile(t, record))
	assertContainsAll(t, "stdin run", contents, []string{"INPUT\tpiped prompt"})
	lines := strings.Split(strings.TrimSpace(contents), "\n")
	assertRunCall(t, lines[1], "run\t--output\ttext")
}

func assertRunStdout(t *testing.T, stdout string) {
	t.Helper()
	if stdout != "agent-result\n" {
		t.Fatalf("stdout = %q, want only the agent result", stdout)
	}
}

func recordedRunLines(t *testing.T, record string) []string {
	t.Helper()
	lines := strings.Split(strings.TrimSpace(string(mustReadTestFile(t, record))), "\n")
	if len(lines) != 4 {
		t.Fatalf("recorded calls = %q, want compile, run, input, and environment", lines)
	}
	return lines
}

func assertRunCall(t *testing.T, line, arguments string) string {
	t.Helper()
	fields := strings.Split(line, "\t")
	if len(fields) < 3 || filepath.Base(fields[1]) != "harnest-agent" ||
		strings.Join(fields[2:], "\t") != arguments {
		t.Fatalf("run call = %q", line)
	}
	return fields[1]
}

func assertEphemeralArtifactRemoved(t *testing.T, launcher string) {
	t.Helper()
	if _, statErr := os.Stat(filepath.Dir(filepath.Dir(launcher))); !os.IsNotExist(statErr) {
		t.Fatalf("ephemeral artifact root was not removed: %v", statErr)
	}
}

func TestRunRejectsAmbiguousOrInvalidInputBeforeCompilation(t *testing.T) {
	tests := []struct {
		name      string
		stdin     string
		arguments []string
		want      string
	}{
		{
			name: "both sources", stdin: "piped prompt", arguments: []string{"run", "agent", "argument prompt"},
			want: "mutually exclusive",
		},
		{
			name: "missing message", arguments: []string{"run", "agent"},
			want: "MESSAGE is required",
		},
		{
			name: "empty positional", arguments: []string{"run", "agent", "   "},
			want: "MESSAGE cannot be empty",
		},
		{
			name: "invalid output", arguments: []string{"run", "agent", "hello", "--output", "yaml"},
			want: "--output must be text, json, or ndjson",
		},
		{
			name: "empty session", arguments: []string{"run", "agent", "hello", "--session", ""},
			want: "--session cannot be empty",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, _, err := executeRunForTest(t, strings.NewReader(test.stdin), test.arguments...)
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("error = %v, want %q", err, test.want)
			}
		})
	}
}

func TestRunStdinPreservesAuthoredWhitespaceExceptShellNewline(t *testing.T) {
	message, err := readRequiredRunMessage(strings.NewReader("  first line\nsecond line  \r\n"))
	if err != nil {
		t.Fatal(err)
	}
	if message != "  first line\nsecond line  " {
		t.Fatalf("message = %q", message)
	}
}

func TestRunRejectsOversizedStdinBeforeCompilation(t *testing.T) {
	_, err := readRequiredRunMessage(
		strings.NewReader(strings.Repeat("x", maxRunMessageBytes+1)),
	)
	if err == nil || !strings.Contains(err.Error(), "4 MiB") {
		t.Fatalf("error = %v, want bounded input failure", err)
	}
}

func TestRunRequiresExplicitCLIInterfaceBeforeCompilation(t *testing.T) {
	target := filepath.Join(t.TempDir(), "server-only-agent")
	if err := createScaffold(target, "server-only-agent"); err != nil {
		t.Fatal(err)
	}
	record := filepath.Join(t.TempDir(), "calls.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	python := writeExecutable(t, `#!/bin/sh
printf 'called\n' >> "$HARNEST_TEST_RECORD"
`)
	_, _, err := executeRunForTest(
		t, strings.NewReader(""),
		"--python", python, "run", target, "hello",
	)
	if err == nil || !strings.Contains(err.Error(), "spec.interfaces.cli: true") {
		t.Fatalf("error = %v, want CLI opt-in guidance", err)
	}
	if _, statErr := os.Stat(record); !os.IsNotExist(statErr) {
		t.Fatalf("compiler was called before CLI policy rejection: %v", statErr)
	}
}

func TestCompileDelegatesCLIInterfaceOptIn(t *testing.T) {
	target := filepath.Join(t.TempDir(), "cli-agent")
	if err := createScaffold(target, "cli-agent"); err != nil {
		t.Fatal(err)
	}
	enableCLIInterface(t, target)
	record := filepath.Join(t.TempDir(), "arguments.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	python := writeExecutable(t, `#!/bin/sh
printf '%s\n' "$@" > "$HARNEST_TEST_RECORD"
`)
	output := filepath.Join(t.TempDir(), "artifact")
	if _, _, err := executeForTest(
		t, defaultSystem(), "--python", python,
		"compile", target, "--output", output,
	); err != nil {
		t.Fatal(err)
	}
	arguments := string(mustReadTestFile(t, record))
	if !strings.Contains(arguments, "--enable-cli") {
		t.Fatalf("compile arguments omitted CLI opt-in:\n%s", arguments)
	}
}

func runRecordingFixture(t *testing.T) (string, string, string) {
	t.Helper()
	target := filepath.Join(t.TempDir(), "run-agent")
	if err := createScaffold(target, "run-agent"); err != nil {
		t.Fatal(err)
	}
	enableCLIInterface(t, target)
	record := filepath.Join(t.TempDir(), "calls.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	python := writeExecutable(t, `#!/bin/sh
printf 'CALL' >> "$HARNEST_TEST_RECORD"
for value in "$@"; do printf '\t%s' "$value" >> "$HARNEST_TEST_RECORD"; done
printf '\n' >> "$HARNEST_TEST_RECORD"
if [ "$1" = "-m" ]; then
  output=""
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--output" ]; then shift; output="$1"; fi
    shift
  done
  mkdir -p "$output"
  printf 'generated launcher\n' > "$output/harnest-agent"
  printf 'compiler-noise\n'
  printf 'compiler-diagnostic\n' >&2
  exit 0
fi
IFS= read -r payload || true
printf 'INPUT\t%s\n' "$payload" >> "$HARNEST_TEST_RECORD"
printf 'ENV\t%s\t%s\n' "$OPENAI_MODEL" "$OPENAI_BASE_URL" >> "$HARNEST_TEST_RECORD"
printf 'agent-result\n'
printf 'agent-diagnostic\n' >&2
`)
	return target, record, python
}

func enableCLIInterface(t *testing.T, target string) {
	t.Helper()
	config, err := os.OpenFile(
		filepath.Join(target, "config.yaml"), os.O_APPEND|os.O_WRONLY, 0,
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := config.WriteString("  interfaces:\n    cli: true\n"); err != nil {
		_ = config.Close()
		t.Fatal(err)
	}
	if err := config.Close(); err != nil {
		t.Fatal(err)
	}
}

func executeRunForTest(
	t *testing.T, stdin *strings.Reader, arguments ...string,
) (string, string, error) {
	t.Helper()
	command := newRootCommand(defaultSystem(), "test-version")
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	command.SetOut(&stdout)
	command.SetErr(&stderr)
	command.SetIn(stdin)
	command.SetArgs(arguments)
	err := command.ExecuteContext(context.Background())
	return stdout.String(), stderr.String(), err
}
