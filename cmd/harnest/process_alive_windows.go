//go:build windows

// Windows process liveness probe used to reap stale environment locks and leases after crashes.

package main

import "syscall"

const (
	processQueryLimitedInformation = 0x1000
	processStillActive             = 259
)

// processAlive reports whether a positive Windows PID still refers to a running process.
func processAlive(pid int) bool {
	// Reject non-positive PIDs before issuing a kernel handle lookup.
	if pid <= 0 {
		return false
	}
	handle, err := syscall.OpenProcess(processQueryLimitedInformation, false, uint32(pid))
	if err != nil {
		// Access denied still indicates an existing process owned by another context.
		return err == syscall.ERROR_ACCESS_DENIED
	}
	defer syscall.CloseHandle(handle)
	var code uint32
	if err := syscall.GetExitCodeProcess(handle, &code); err != nil {
		return false
	}
	return code == processStillActive
}
