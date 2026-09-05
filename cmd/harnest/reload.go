package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/engine"
)

const (
	reloadPollInterval = 200 * time.Millisecond
	reloadDebounce     = 300 * time.Millisecond
	reloadStartupGrace = 300 * time.Millisecond
	reloadSignalGrace  = 500 * time.Millisecond
	reloadStopTimeout  = 10 * time.Second
)

type reloadGeneration struct {
	number   int
	artifact string
	bundle   engine.Bundle
	python   pythonSelection
}

type reloadProcess struct {
	generation reloadGeneration
	command    *exec.Cmd
	done       chan struct{}
	waitError  error
}

type reloadWatchState struct {
	baseline       string
	candidate      string
	candidateSince time.Time
	lastError      string
}

type reloadSupervisor struct {
	application *application
	command     *cobra.Command
	options     serveOptions
	source      string
	root        string
	next        int
	current     *reloadProcess
	watch       reloadWatchState
}

// serveReload owns ephemeral artifact generations around one development server.
func (a *application) serveReload(
	command *cobra.Command, bundle engine.Bundle, options serveOptions,
) error {
	root, err := os.MkdirTemp("", "harnest-reload-")
	if err != nil {
		return fmt.Errorf("create reload artifact root: %w", err)
	}
	defer os.RemoveAll(root)
	generation, err := a.prepareReloadGeneration(command, bundle.Directory, root, 1)
	if err != nil {
		return err
	}
	process, err := a.startReloadGeneration(command, generation, options)
	if err != nil {
		return err
	}
	fmt.Fprintf(
		command.ErrOrStderr(),
		"Serving %s with development reload on %s (generation 1)\n",
		generation.bundle.Config.Metadata.Name,
		options.host,
	)
	supervisor := reloadSupervisor{
		application: a,
		command:     command,
		options:     options,
		source:      bundle.Directory,
		root:        root,
		next:        2,
		current:     process,
		watch:       reloadWatchState{baseline: generation.bundle.Digest},
	}
	return supervisor.run(command.Context())
}

// prepareReloadGeneration resolves dependencies before compiling an immutable tree.
func (a *application) prepareReloadGeneration(
	command *cobra.Command, source, root string, number int,
) (reloadGeneration, error) {
	bundle, python, err := a.reloadBundleAndPython(command, source)
	if err != nil {
		return reloadGeneration{}, err
	}
	artifact := filepath.Join(root, fmt.Sprintf("generation-%06d", number))
	if err := a.compileBundle(command, python, bundle, artifact, command.InOrStdin()); err != nil {
		_ = os.RemoveAll(artifact)
		return reloadGeneration{}, err
	}
	if _, err := compiledLauncher(artifact); err != nil {
		_ = os.RemoveAll(artifact)
		return reloadGeneration{}, err
	}
	return reloadGeneration{
		number: number, artifact: artifact, bundle: bundle, python: python,
	}, nil
}

// reloadBundleAndPython lets dependency synchronization settle before compilation.
func (a *application) reloadBundleAndPython(
	command *cobra.Command, source string,
) (engine.Bundle, pythonSelection, error) {
	bundle, err := loadAgentBundle(source)
	if err != nil {
		return engine.Bundle{}, pythonSelection{}, err
	}
	python, err := a.agentPython(command, bundle)
	if err != nil {
		return engine.Bundle{}, pythonSelection{}, err
	}
	refreshed, err := loadAgentBundle(source)
	if err != nil {
		return engine.Bundle{}, pythonSelection{}, err
	}
	if refreshed.Digest == bundle.Digest {
		return refreshed, python, nil
	}
	// Environment synchronization may update harnest-runtime.lock. Resolve once more so the
	// compiled generation and interpreter share the final dependency identity.
	python, err = a.agentPython(command, refreshed)
	if err != nil {
		return engine.Bundle{}, pythonSelection{}, err
	}
	final, err := loadAgentBundle(source)
	return final, python, err
}

// startReloadGeneration starts one process without coupling it to parent cancellation.
func (a *application) startReloadGeneration(
	command *cobra.Command, generation reloadGeneration, options serveOptions,
) (*reloadProcess, error) {
	launcher, err := compiledLauncher(generation.artifact)
	if err != nil {
		return nil, err
	}
	child := a.system.commandContext(
		context.Background(), generation.python.Executable, options.arguments(launcher)...,
	)
	child.Stdin = command.InOrStdin()
	child.Stdout = command.OutOrStdout()
	child.Stderr = command.ErrOrStderr()
	child.Env = configuredEnvironment(generation.bundle)
	if err := child.Start(); err != nil {
		return nil, fmt.Errorf("start reload generation %d: %w", generation.number, err)
	}
	process := &reloadProcess{
		generation: generation,
		command:    child,
		done:       make(chan struct{}),
	}
	go func() {
		process.waitError = child.Wait()
		close(process.done)
	}()
	return process, nil
}

// run watches source identity while separately observing process termination.
func (s *reloadSupervisor) run(ctx context.Context) error {
	ticker := time.NewTicker(reloadPollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			s.stopCurrent()
			return nil
		case <-s.current.done:
			if ctx.Err() != nil {
				return nil
			}
			return unexpectedReloadExit(
				s.current.generation.number,
				s.current.waitError,
			)
		case now := <-ticker.C:
			if err := s.poll(now); err != nil {
				s.stopCurrent()
				return err
			}
		}
	}
}

// poll debounces a complete source digest rather than reacting to partial writes.
func (s *reloadSupervisor) poll(now time.Time) error {
	digest, err := engine.BundleDigest(s.source)
	if err != nil {
		s.reportWatchError(err)
		return nil
	}
	s.watch.lastError = ""
	if !s.watch.ready(digest, now) {
		return nil
	}
	candidate := s.watch.candidate
	generation, err := s.application.prepareReloadGeneration(
		s.command, s.source, s.root, s.next,
	)
	s.next++
	if err != nil {
		s.watch.reset(candidate)
		fmt.Fprintf(
			s.command.ErrOrStderr(),
			"Reload failed; generation %d remains active: %v\n",
			s.current.generation.number,
			err,
		)
		return nil
	}
	s.watch.reset(generation.bundle.Digest)
	return s.replace(generation)
}

// replace stops the old generation only after the replacement compiled successfully.
func (s *reloadSupervisor) replace(generation reloadGeneration) error {
	previous := s.current
	if err := previous.stop(reloadStopTimeout); err != nil {
		fmt.Fprintf(s.command.ErrOrStderr(), "Reload shutdown warning: %v\n", err)
	}
	replacement, err := s.application.startReloadGeneration(
		s.command, generation, s.options,
	)
	if err == nil {
		err = replacement.awaitStartup(s.command.Context(), reloadStartupGrace)
	}
	if err != nil {
		if replacement != nil {
			_ = replacement.stop(reloadStopTimeout)
		}
		_ = os.RemoveAll(generation.artifact)
		if s.command.Context().Err() != nil {
			if replacement != nil {
				s.current = replacement
			} else {
				s.current = previous
			}
			return nil
		}
		return s.restore(previous.generation, err)
	}
	s.current = replacement
	_ = os.RemoveAll(previous.generation.artifact)
	fmt.Fprintf(
		s.command.ErrOrStderr(),
		"Reloaded %s (generation %d)\n",
		generation.bundle.Config.Metadata.Name,
		generation.number,
	)
	return nil
}

// restore restarts the last known-good artifact when process creation fails.
func (s *reloadSupervisor) restore(
	previous reloadGeneration, replacementError error,
) error {
	rollback, rollbackError := s.application.startReloadGeneration(
		s.command, previous, s.options,
	)
	if rollbackError != nil {
		return fmt.Errorf(
			"start replacement: %v; restore generation %d: %w",
			replacementError,
			previous.number,
			rollbackError,
		)
	}
	s.current = rollback
	fmt.Fprintf(
		s.command.ErrOrStderr(),
		"Reload start failed; restored generation %d: %v\n",
		previous.number,
		replacementError,
	)
	return nil
}

// stopCurrent reports shutdown degradation without turning cancellation into failure.
func (s *reloadSupervisor) stopCurrent() {
	// Terminal signals normally reach the foreground child too. Give that
	// signal time to finish before issuing the fallback used by API cancellation.
	if s.current.wait(reloadSignalGrace) {
		return
	}
	if err := s.current.stop(reloadStopTimeout); err != nil {
		fmt.Fprintf(s.command.ErrOrStderr(), "Reload shutdown warning: %v\n", err)
	}
}

// reportWatchError suppresses repeated filesystem diagnostics until state changes.
func (s *reloadSupervisor) reportWatchError(err error) {
	message := err.Error()
	if message == s.watch.lastError {
		return
	}
	s.watch.lastError = message
	fmt.Fprintf(s.command.ErrOrStderr(), "Reload watch warning: %v\n", err)
}

// ready requires one unchanged digest for the debounce period.
func (s *reloadWatchState) ready(digest string, now time.Time) bool {
	if digest == s.baseline {
		s.candidate = ""
		return false
	}
	if digest != s.candidate {
		s.candidate = digest
		s.candidateSince = now
		return false
	}
	return now.Sub(s.candidateSince) >= reloadDebounce
}

// reset commits the source identity handled by the last reload attempt.
func (s *reloadWatchState) reset(baseline string) {
	s.baseline = baseline
	s.candidate = ""
	s.candidateSince = time.Time{}
}

// wait reports whether a process exits within a bounded observation window.
func (p *reloadProcess) wait(timeout time.Duration) bool {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-p.done:
		return true
	case <-timer.C:
		return false
	}
}

// awaitStartup detects immediate configuration or bind failures before commit.
func (p *reloadProcess) awaitStartup(ctx context.Context, grace time.Duration) error {
	timer := time.NewTimer(grace)
	defer timer.Stop()
	select {
	case <-p.done:
		return unexpectedReloadExit(p.generation.number, p.waitError)
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// stop requests graceful lifecycle shutdown before using a bounded hard stop.
func (p *reloadProcess) stop(timeout time.Duration) error {
	select {
	case <-p.done:
		return nil
	default:
	}
	if err := p.command.Process.Signal(os.Interrupt); err != nil {
		return fmt.Errorf("signal generation %d: %w", p.generation.number, err)
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-p.done:
		return nil
	case <-timer.C:
		if err := p.command.Process.Kill(); err != nil {
			return fmt.Errorf("kill generation %d: %w", p.generation.number, err)
		}
		<-p.done
		return fmt.Errorf("generation %d exceeded the graceful shutdown timeout", p.generation.number)
	}
}

// unexpectedReloadExit preserves process failure identity without treating cancellation as failure.
func unexpectedReloadExit(number int, err error) error {
	if err == nil {
		return fmt.Errorf("reload generation %d exited unexpectedly", number)
	}
	return fmt.Errorf("reload generation %d exited unexpectedly: %w", number, err)
}
