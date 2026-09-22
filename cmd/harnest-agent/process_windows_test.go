//go:build windows

package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/windows"
)

// TestWindowsProcessHelper runs subprocess fixtures in the native test executable.
func TestWindowsProcessHelper(t *testing.T) {
	switch os.Getenv("HARNEST_PROCESS_HELPER") {
	case "launcher":
		command := windowsHelper("child")
		code, err := waitAgent(command)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		os.Exit(code)
	case "child":
		grandchild := windowsHelper("grandchild")
		if err := grandchild.Start(); err != nil {
			os.Exit(2)
		}
		data := fmt.Sprintf("%d %d", os.Getpid(), grandchild.Process.Pid)
		if err := os.WriteFile(os.Getenv("HARNEST_PROCESS_READY"), []byte(data), 0600); err != nil {
			os.Exit(3)
		}
		time.Sleep(time.Minute)
	case "grandchild":
		time.Sleep(time.Minute)
	case "exit":
		os.Exit(23)
	}
}

// windowsHelper isolates process environment selection from launcher lifecycle code.
func windowsHelper(mode string) *exec.Cmd {
	command := exec.Command(os.Args[0], "-test.run=^TestWindowsProcessHelper$")
	command.Env = append(os.Environ(), "HARNEST_PROCESS_HELPER="+mode)
	return command
}

// TestWindowsWaitPreservesExitStatus verifies native child status without shell translation.
func TestWindowsWaitPreservesExitStatus(t *testing.T) {
	code, err := waitAgent(windowsHelper("exit"))
	if err != nil || code != 23 {
		t.Fatalf("want exit 23, got %d: %v", code, err)
	}
}

// TestWindowsForcedLauncherExitKillsDescendants verifies that a terminated native
// launcher cannot leave Python or subprocess tools behind on a deployment host.
func TestWindowsForcedLauncherExitKillsDescendants(t *testing.T) {
	marker := filepath.Join(t.TempDir(), "ready")
	command := windowsHelper("launcher")
	command.Env = append(command.Env, "HARNEST_PROCESS_READY="+marker)
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = command.Process.Kill(); _ = command.Wait() }()
	processes := waitWindowsDescendants(t, marker)
	for _, handle := range processes {
		defer windows.CloseHandle(handle)
	}
	if err := command.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	for _, handle := range processes {
		status, err := windows.WaitForSingleObject(handle, 10000)
		if err != nil || status != windows.WAIT_OBJECT_0 {
			t.Fatalf("descendant survived launcher: %d: %v", status, err)
		}
	}
}

// waitWindowsDescendants retains kernel handles before killing the launcher,
// avoiding PID reuse and proving actual child termination rather than lookup failure.
func waitWindowsDescendants(t *testing.T, marker string) []windows.Handle {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		data, err := os.ReadFile(marker)
		if err == nil && len(strings.Fields(string(data))) == 2 {
			return openWindowsDescendants(t, string(data))
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("process fixture did not become ready")
	return nil
}

// openWindowsDescendants acquires wait-only handles without granting process mutation.
func openWindowsDescendants(t *testing.T, data string) []windows.Handle {
	t.Helper()
	var result []windows.Handle
	for _, value := range strings.Fields(data) {
		pid, err := strconv.ParseUint(value, 10, 32)
		if err != nil {
			t.Fatal(err)
		}
		handle, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(pid))
		if err != nil {
			t.Fatal(err)
		}
		result = append(result, handle)
	}
	return result
}
