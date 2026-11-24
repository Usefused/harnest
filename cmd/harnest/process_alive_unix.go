//go:build !windows

// Unix process liveness probe used to reap stale environment locks and leases after crashes.

package main

import (
	"errors"
	"syscall"
)

// processAlive reports whether a positive Unix PID still refers to a live process.
func processAlive(pid int) bool {
	// Reject non-positive PIDs so signal 0 never probes a process group or all processes.
	if pid <= 0 {
		return false
	}
	err := syscall.Kill(pid, 0)
	// EPERM means the process exists under another user; ESRCH means it has exited.
	return err == nil || errors.Is(err, syscall.EPERM)
}
