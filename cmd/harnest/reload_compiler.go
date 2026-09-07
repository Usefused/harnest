package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os/exec"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/engine"
)

const reloadCompilerStopTimeout = 2 * time.Second

type reloadCompileRequest struct {
	ID         string `json:"id"`
	Source     string `json:"source"`
	Output     string `json:"output"`
	Entrypoint string `json:"entrypoint"`
	Framework  string `json:"framework"`
	Mode       string `json:"mode"`
	CLIEnabled bool   `json:"cliEnabled"`
}

type reloadCompileResponse struct {
	ID     string `json:"id"`
	OK     bool   `json:"ok"`
	Digest string `json:"digest"`
	Error  string `json:"error"`
}

type reloadCompiler struct {
	identity string
	command  *exec.Cmd
	stdin    io.WriteCloser
	encoder  *json.Encoder
	decoder  *json.Decoder
	done     chan error
	python   pythonSelection
}

type reloadCompilerPool struct {
	application *application
	command     *cobra.Command
	current     *reloadCompiler
	request     int
}

// compile uses a warm managed compiler while preserving custom-Python compatibility.
func (p *reloadCompilerPool) compile(
	bundle engine.Bundle, python pythonSelection, artifact string,
) error {
	if !managedServeArtifactCacheAvailable(python) {
		return p.application.compileBundle(
			p.command, python, bundle, artifact, p.command.InOrStdin(),
		)
	}
	compiler, err := p.compilerFor(bundle, python)
	if err != nil {
		return err
	}
	p.request++
	request := reloadCompileRequest{
		ID:         fmt.Sprintf("reload-%d", p.request),
		Source:     bundle.Directory,
		Output:     artifact,
		Entrypoint: bundle.Config.Spec.Entrypoint,
		Framework:  bundle.Config.Spec.Framework.Name,
		Mode:       bundle.Config.Spec.Framework.EffectiveMode(),
		CLIEnabled: bundle.Config.Spec.Interfaces.CLI,
	}
	usable, err := compiler.compile(request)
	if !usable {
		// Protocol damage can desynchronize later responses; a normal graph
		// validation error leaves the warm compiler safe for the next edit.
		compiler.close()
		p.current = nil
	}
	if err != nil {
		return fmt.Errorf("compile reload generation: %w", err)
	}
	return nil
}

// compilerFor replaces the daemon when its interpreter or configured environment changes.
func (p *reloadCompilerPool) compilerFor(
	bundle engine.Bundle, python pythonSelection,
) (*reloadCompiler, error) {
	environment := configuredEnvironment(bundle)
	identity := reloadCompilerIdentity(python.Executable, environment)
	if p.current != nil && p.current.identity == identity {
		return p.current, nil
	}
	if p.current != nil {
		p.current.close()
		p.current = nil
	}
	compiler, err := p.start(bundle.Directory, python, environment, identity)
	if err != nil {
		return nil, err
	}
	p.current = compiler
	return compiler, nil
}

// start launches the private compiler protocol and holds its environment lease.
func (p *reloadCompilerPool) start(
	project string, python pythonSelection, environment []string, identity string,
) (*reloadCompiler, error) {
	leased, err := leaseAgentPython(
		project,
		pythonSelection{Executable: python.Executable, Source: python.Source},
	)
	if err != nil {
		return nil, err
	}
	child := p.application.system.commandContext(
		p.command.Context(), python.Executable, "-m", "harnest.compiler_daemon",
	)
	child.Env = environment
	child.Stderr = p.command.ErrOrStderr()
	stdin, err := child.StdinPipe()
	if err != nil {
		leased.releaseLease()
		return nil, fmt.Errorf("open reload compiler input: %w", err)
	}
	stdout, err := child.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		leased.releaseLease()
		return nil, fmt.Errorf("open reload compiler output: %w", err)
	}
	if err := child.Start(); err != nil {
		_ = stdin.Close()
		leased.releaseLease()
		return nil, fmt.Errorf("start reload compiler: %w", err)
	}
	compiler := &reloadCompiler{
		identity: identity,
		command:  child,
		stdin:    stdin,
		encoder:  json.NewEncoder(stdin),
		decoder:  json.NewDecoder(stdout),
		done:     make(chan error, 1),
		python:   leased,
	}
	go func() { compiler.done <- child.Wait() }()
	return compiler, nil
}

// compile performs one request-response exchange on the serial supervisor channel.
func (c *reloadCompiler) compile(request reloadCompileRequest) (bool, error) {
	if err := c.encoder.Encode(request); err != nil {
		return false, fmt.Errorf("send compiler request: %w", err)
	}
	var response reloadCompileResponse
	if err := c.decoder.Decode(&response); err != nil {
		return false, fmt.Errorf("read compiler response: %w", err)
	}
	if response.ID != request.ID {
		return false, fmt.Errorf(
			"compiler response id %q does not match request %q",
			response.ID,
			request.ID,
		)
	}
	if !response.OK {
		message := strings.TrimSpace(response.Error)
		if message == "" {
			message = "compiler rejected the request without an error"
		}
		return true, fmt.Errorf("%s", message)
	}
	if strings.TrimSpace(response.Digest) == "" {
		return false, fmt.Errorf("compiler response omitted the artifact digest")
	}
	return true, nil
}

// close ends the compiler gracefully and releases its managed environment lease.
func (c *reloadCompiler) close() {
	_ = c.stdin.Close()
	timer := time.NewTimer(reloadCompilerStopTimeout)
	defer timer.Stop()
	select {
	case <-c.done:
	case <-timer.C:
		_ = c.command.Process.Kill()
		<-c.done
	}
	c.python.releaseLease()
}

// close relinquishes the one daemon owned by this reload supervisor.
func (p *reloadCompilerPool) close() {
	if p.current == nil {
		return
	}
	p.current.close()
	p.current = nil
}

// reloadCompilerIdentity restarts the process when compile-time environment changes.
func reloadCompilerIdentity(python string, environment []string) string {
	digest := sha256.New()
	digest.Write([]byte(python))
	for _, value := range environment {
		digest.Write([]byte{0})
		digest.Write([]byte(value))
	}
	return hex.EncodeToString(digest.Sum(nil))
}
