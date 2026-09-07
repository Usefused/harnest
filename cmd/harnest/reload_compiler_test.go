package main

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func TestReloadCompilerPoolReusesManagedCompilerProcess(t *testing.T) {
	target, _, _ := serveRecordingFixture(t)
	bundle, err := loadAgentBundle(target)
	if err != nil {
		t.Fatal(err)
	}
	record := filepath.Join(t.TempDir(), "compiler-record.txt")
	t.Setenv("HARNEST_TEST_COMPILER_RECORD", record)
	t.Setenv("HARNEST_TEST_COMPILER_FAIL_FIRST", "1")
	python := pythonSelection{
		Executable: writeExecutable(t, reloadCompilerTestPython),
		Source:     "agent environment",
	}
	command := &cobra.Command{}
	command.SetContext(context.Background())
	command.SetIn(strings.NewReader(""))
	command.SetOut(io.Discard)
	command.SetErr(io.Discard)
	pool := &reloadCompilerPool{
		application: &application{system: defaultSystem()},
		command:     command,
	}

	if err := pool.compile(bundle, python, filepath.Join(target, ".harnest", "one")); err == nil ||
		!strings.Contains(err.Error(), "invalid graph") {
		t.Fatalf("got first compile error %v, want invalid graph", err)
	}
	if err := pool.compile(bundle, python, filepath.Join(target, ".harnest", "two")); err != nil {
		t.Fatal(err)
	}
	pool.close()

	contents, err := os.ReadFile(record)
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Count(string(contents), "START\n"); got != 1 {
		t.Fatalf("compiler started %d times, want one:\n%s", got, contents)
	}
	if got := strings.Count(string(contents), "REQUEST\n"); got != 2 {
		t.Fatalf("compiler handled %d requests, want two:\n%s", got, contents)
	}
	if got := strings.Count(string(contents), "STOP\n"); got != 1 {
		t.Fatalf("compiler stopped %d times, want one:\n%s", got, contents)
	}
}

const reloadCompilerTestPython = `#!/bin/sh
record="$HARNEST_TEST_COMPILER_RECORD"
printf 'START\n' >> "$record"
count=0
while IFS= read -r request; do
  count=$((count + 1))
  printf 'REQUEST\n' >> "$record"
  if [ "$count" = 1 ] && [ "$HARNEST_TEST_COMPILER_FAIL_FIRST" = 1 ]; then
    printf '{"id":"reload-1","ok":false,"error":"invalid graph"}\n'
  else
    printf '{"id":"reload-%s","ok":true,"digest":"compiled"}\n' "$count"
  fi
done
printf 'STOP\n' >> "$record"
`
