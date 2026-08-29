package main

import (
	"context"
	"io"
	"os"
	"os/exec"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/internal/runtimewheel"
	"harnest.dev/harnest/internal/uvbootstrap"
)

const rootDescription = `Harnest builds self-contained ADK or LangGraph agents from ordinary folders.

Each agent owns its deployment config, Agent Card, instructions, Python
definition, discovered tools and subagents, evaluations, and authored tests:

  my-agent/
    config.yaml          deployment resources and runtime policy
    server.yaml          mutable compiled-server policy
    agent-card.yaml      discovery metadata and capabilities
    agent.py             root Agent with compiler-owned imports
    instructions.md      root instructions
    pyproject.toml       one dependency set for the agent and runtime plugins
    lib/                 reusable Python helpers imported as harnest.lib.*
    models/              Pydantic contracts imported as harnest.models.*
    tools/               discovered Python tools
    plugins/             runtime plugins or manifest-less MCP+skill agent-plugins
    extensions/          portable lifecycle plus framework-specific integration
    subagents/           discovered Agent definitions
    mcp/                 MCPClient connections to external MCP servers
    sandbox/             optional ADK isolated code-execution backend
    skills/              progressively loaded agent skills
    evals/               ADK eval sets or authored pytest evals
    tests/{unit,smoke}/   authored pytest suites

Typical workflow:

  harnest skills install
  harnest init my-agent --framework adk
  harnest init my-graph --framework langgraph
  harnest init example-agent --framework adk --example
  harnest env sync my-agent
  harnest mode advanced my-agent --check
  harnest upgrade my-agent
  harnest upgrade my-agent --apply
  harnest test my-agent
  harnest test my-agent --smoke --evals
  harnest test my-agent --evals --eval-trajectory strict
  harnest compile my-agent --output .harnest/my-agent
  harnest serve my-agent

Released compile, test, and serve commands use an isolated environment derived
from config.yaml, pyproject.toml, uv.lock, and the embedded Harnest wheel.
Runtime plugins declare plugin.yaml kind RuntimePlugin, export plugin from
plugin.py, and share that interpreter; their module is harnest.plugins.<name>.
CLI Python is selected from --python, HARNEST_PYTHON, the private Harnest
runtime, or python3 on PATH.

The bundled authoring skill is project-local guidance for coding agents. It is
never compiled as one of the generated agent's runtime skills.`

type system struct {
	getenv         func(string) string
	userHomeDir    func() (string, error)
	lookPath       func(string) (string, error)
	commandContext func(context.Context, string, ...string) *exec.Cmd
	embeddedWheel  func(string) (runtimewheel.Artifact, error)
	embeddedUV     func() (uvbootstrap.Artifact, error)
}

func defaultSystem() system {
	return system{
		getenv:         os.Getenv,
		userHomeDir:    os.UserHomeDir,
		lookPath:       exec.LookPath,
		commandContext: exec.CommandContext,
		embeddedWheel:  runtimewheel.Embedded,
		embeddedUV:     uvbootstrap.Embedded,
	}
}

type application struct {
	system     system
	version    string
	pythonFlag string
}

func newRootCommand(sys system, cliVersion string) *cobra.Command {
	app := &application{system: sys, version: cliVersion}
	command := &cobra.Command{
		Use:           "harnest",
		Short:         "Build, test, and run filesystem-first agent graphs",
		Long:          rootDescription,
		Version:       cliVersion,
		SilenceErrors: true,
		SilenceUsage:  true,
		CompletionOptions: cobra.CompletionOptions{
			DisableDefaultCmd: true,
		},
	}
	command.PersistentFlags().StringVar(
		&app.pythonFlag,
		"python",
		"",
		"Python executable (overrides HARNEST_PYTHON and the managed runtime)",
	)
	command.AddCommand(
		app.newInitCommand(),
		app.newCompileCommand(),
		app.newTestCommand(),
		app.newServeCommand(),
		app.newDoctorCommand(),
		app.newRuntimeCommand(),
		app.newEnvironmentCommand(),
		app.newSkillsCommand(),
		app.newModeCommand(),
		app.newUpgradeCommand(),
	)
	return command
}

func runCommand(command *exec.Cmd, stdin io.Reader, stdout, stderr io.Writer) error {
	command.Stdin = stdin
	command.Stdout = stdout
	command.Stderr = stderr
	return command.Run()
}
