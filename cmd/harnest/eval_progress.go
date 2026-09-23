package main

import (
	"fmt"
	"io"
	"sync"
	"time"

	"github.com/spf13/cobra"
	"golang.org/x/term"
)

// evalProgress owns stderr activity while preserving subprocess output verbatim.
type evalProgress struct {
	mu       sync.Mutex
	stderr   io.Writer
	terminal bool
	stage    string
	started  time.Time
	lastLog  time.Time
	frame    int
	visible  bool
	partial  [2]bool
	stop     chan struct{}
	done     chan struct{}
	restore  func()
}

// startEvalProgress covers environment setup as well as the Python evaluator.
func startEvalProgress(command *cobra.Command, enabled bool, terminalName string) *evalProgress {
	if !enabled {
		return nil
	}
	stdout, stderr := command.OutOrStdout(), command.ErrOrStderr()
	p := &evalProgress{
		stderr: stderr, terminal: terminalName != "dumb" && evalTerminal(stderr),
		started: time.Now(), stop: make(chan struct{}), done: make(chan struct{}),
	}
	// Both streams share the rendering lock: provider warnings and test output
	// must clear the activity line before writing, including concurrent pipes.
	command.SetOut(evalProgressWriter{p, stdout, 0})
	command.SetErr(evalProgressWriter{p, stderr, 1})
	p.restore = func() { command.SetOut(stdout); command.SetErr(stderr) }
	p.phase("Preparing evaluation environment")
	go p.run()
	return p
}

// evalTerminal excludes pipes, files, and non-console character devices.
func evalTerminal(writer io.Writer) bool {
	file, ok := writer.(interface{ Fd() uintptr })
	return ok && term.IsTerminal(int(file.Fd()))
}

// phase emits an immediate durable milestone before a potentially slow step.
func (p *evalProgress) phase(stage string) {
	if p == nil {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.clear()
	p.stage = stage
	p.lastLog = time.Now()
	fmt.Fprintf(p.stderr, "harnest eval: %s\n", stage)
}

// run keeps activity independent of Python imports, blocking calls, and output.
func (p *evalProgress) run() {
	defer close(p.done)
	ticker := time.NewTicker(150 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case now := <-ticker.C:
			p.tick(now)
		case <-p.stop:
			return
		}
	}
}

// tick animates terminals and emits sparse plain-text heartbeats in CI logs.
func (p *evalProgress) tick(now time.Time) {
	p.mu.Lock()
	defer p.mu.Unlock()
	// Do not overwrite an unfinished line (for example pytest's progress dots).
	if p.partial[0] || p.partial[1] {
		return
	}
	elapsed := now.Sub(p.started).Truncate(time.Second)
	if p.terminal {
		fmt.Fprintf(p.stderr, "\r\x1b[2K%c %s (%s elapsed)", "|/-\\"[p.frame%4], p.stage, elapsed)
		p.frame++
		p.visible = true
	} else if now.Sub(p.lastLog) >= 15*time.Second {
		fmt.Fprintf(p.stderr, "harnest eval: still running (%s elapsed)\n", elapsed)
		p.lastLog = now
	}
}

// clear removes only an activity line owned by this renderer; mu must be held.
func (p *evalProgress) clear() {
	if p.visible {
		fmt.Fprint(p.stderr, "\r\x1b[2K")
		p.visible = false
	}
}

// close joins the renderer before restoring output on success or any failure.
func (p *evalProgress) close() {
	if p == nil {
		return
	}
	close(p.stop)
	<-p.done
	p.mu.Lock()
	defer p.mu.Unlock()
	p.clear()
	p.restore()
}

type evalProgressWriter struct {
	progress *evalProgress
	target   io.Writer
	stream   int
}

// Write clears the spinner without changing stdout bytes or losing write errors.
func (w evalProgressWriter) Write(data []byte) (int, error) {
	p := w.progress
	p.mu.Lock()
	defer p.mu.Unlock()
	p.clear()
	n, err := w.target.Write(data)
	if n > 0 {
		p.partial[w.stream] = data[n-1] != '\n'
	}
	return n, err
}
