package main

import (
	"context"
	"io"
	"os"
	"os/exec"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/internal/runtimewheel"
)

const rootDescription = `Harnest builds self-contained ADK or LangGraph agents from ordinary folders.

Each agent owns its deployment config, Agent Card, instructions, Python
definition, discovered tools and subagents, evaluations, and authored tests:

  my-agent/
    config.yaml          deployment resources and runtime policy
    agent-card.yaml      discovery metadata and capabilities
    agent.py             root Agent with compiler-owned imports
    instructions.md      root instructions
    tools/               discovered Python tools
    plugins/             reusable MCP-client-and-skill capability bundles
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
  harnest mode advanced my-agent --check
  harnest test my-agent
  harnest test my-agent --smoke --evals
  harnest compile my-agent --output .harnest/my-agent
  harnest serve my-agent

Python is selected from --python, HARNEST_PYTHON, the managed Harnest runtime
at ${HARNEST_RUNTIME_DIR:-~/.harnest/runtime}, or python3 on PATH, in that order.

The bundled authoring skill is project-local guidance for coding agents. It is
never compiled as one of the generated agent's runtime skills.`

type system struct {
	getenv         func(string) string
	userHomeDir    func() (string, error)
	lookPath       func(string) (string, error)
	commandContext func(context.Context, string, ...string) *exec.Cmd
	embeddedWheel  func(string) (runtimewheel.Artifact, error)
}

func defaultSystem() system {
	return system{
		getenv:         os.Getenv,
		userHomeDir:    os.UserHomeDir,
		lookPath:       exec.LookPath,
		commandContext: exec.CommandContext,
		embeddedWheel:  runtimewheel.Embedded,
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
		app.newSkillsCommand(),
		app.newModeCommand(),
	)
	return command
}

func runCommand(command *exec.Cmd, stdin io.Reader, stdout, stderr io.Writer) error {
	command.Stdin = stdin
	command.Stdout = stdout
	command.Stderr = stderr
	return command.Run()
}
