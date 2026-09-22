//go:build windows

package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// waitAgent lets the child receive console events while keeping the launcher
// alive to reap it and clean up. A non-inherited job owns the child tree, so
// forced termination of the launcher also closes the job and kills descendants.
func waitAgent(command *exec.Cmd) (int, error) {
	job, err := newAgentJob()
	if err != nil {
		return 1, err
	}
	defer windows.CloseHandle(job)
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)
	if err = startAgentInJob(command, job); err != nil {
		return 1, err
	}
	return windowsExitStatus(command.Wait())
}

// newAgentJob makes descendant cleanup independent of normal launcher shutdown.
func newAgentJob() (windows.Handle, error) {
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return 0, err
	}
	limits := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	limits.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	_, err = windows.SetInformationJobObject(job, windows.JobObjectExtendedLimitInformation, uintptr(unsafe.Pointer(&limits)), uint32(unsafe.Sizeof(limits)))
	if err != nil {
		windows.CloseHandle(job)
		return 0, err
	}
	return job, nil
}

// startAgentInJob suspends the child's initial thread until job ownership is
// established. This prevents fast-starting tools from spawning outside the job.
func startAgentInJob(command *exec.Cmd, job windows.Handle) error {
	if command.SysProcAttr == nil {
		command.SysProcAttr = &syscall.SysProcAttr{}
	}
	command.SysProcAttr.CreationFlags |= windows.CREATE_SUSPENDED
	if err := command.Start(); err != nil {
		return err
	}
	if err := assignAndResumeAgent(job, command.Process.Pid); err != nil {
		_ = command.Process.Kill()
		_ = command.Wait()
		return fmt.Errorf("start agent in Windows job: %w", err)
	}
	return nil
}

// assignAndResumeAgent keeps console inheritance intact for Ctrl+C/Ctrl+Break.
func assignAndResumeAgent(job windows.Handle, pid int) error {
	process, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(pid))
	if err != nil {
		return err
	}
	defer windows.CloseHandle(process)
	if err = windows.AssignProcessToJobObject(job, process); err != nil {
		return err
	}
	return resumeAgentThread(uint32(pid))
}

// resumeAgentThread recovers the suspended initial thread handle that os/exec
// closes after CreateProcess; only the newly created process can be resumed.
func resumeAgentThread(pid uint32) error {
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPTHREAD, 0)
	if err != nil {
		return err
	}
	defer windows.CloseHandle(snapshot)
	entry := windows.ThreadEntry32{}
	entry.Size = uint32(unsafe.Sizeof(entry))
	for err = windows.Thread32First(snapshot, &entry); err == nil; err = windows.Thread32Next(snapshot, &entry) {
		if entry.OwnerProcessID == pid {
			return resumeThread(entry.ThreadID)
		}
	}
	return fmt.Errorf("cannot find agent initial thread: %w", err)
}

// resumeThread releases exactly the one suspension introduced at process creation.
func resumeThread(id uint32) error {
	thread, err := windows.OpenThread(windows.THREAD_SUSPEND_RESUME, false, id)
	if err != nil {
		return err
	}
	defer windows.CloseHandle(thread)
	_, err = windows.ResumeThread(thread)
	return err
}

// windowsExitStatus preserves the native process status for shell callers.
func windowsExitStatus(err error) (int, error) {
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return exit.ExitCode(), nil
	}
	if err != nil {
		return 1, err
	}
	return 0, nil
}
