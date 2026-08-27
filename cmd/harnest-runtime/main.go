package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"

	"harnest.dev/harnest/engine"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	var orchestratorPath string
	var planPath string
	var python string
	var deployCommand string
	var deployArgs string
	var compiledRoot string
	flags := flag.NewFlagSet("harnest-runtime", flag.ContinueOnError)
	flags.SetOutput(stderr)
	flags.StringVar(&orchestratorPath, "orchestrator", "", "Python orchestrator.py to execute")
	flags.StringVar(&planPath, "plan", "", "pre-rendered plan JSON, or - for stdin")
	flags.StringVar(&python, "python", "python3", "Python executable used to render the orchestrator")
	flags.StringVar(&deployCommand, "deploy-command", "", "engine deployment binary (default: dry run)")
	flags.StringVar(&deployArgs, "deploy-args", "", "space-separated arguments passed before each agent directory")
	flags.StringVar(&compiledRoot, "compiled-root", "", "directory for compiled agents (default: temporary and removed after deployment)")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if flags.NArg() != 0 {
		fmt.Fprintf(stderr, "harnest-runtime: unexpected positional arguments: %s\n", strings.Join(flags.Args(), " "))
		return 2
	}

	reader, closer, err := planReader(orchestratorPath, planPath, python, stdin, stderr)
	if err != nil {
		fmt.Fprintln(stderr, "harnest-runtime:", err)
		return 2
	}
	if closer != nil {
		defer closer.Close()
	}
	plan, err := engine.DecodePlan(reader)
	if err != nil {
		fmt.Fprintln(stderr, "harnest-runtime:", err)
		return 2
	}

	var deployer engine.Deployer = engine.DryRunDeployer{Writer: stdout}
	if deployCommand != "" {
		deployer = engine.CommandDeployer{
			Command: deployCommand, Args: strings.Fields(deployArgs), Stdout: stdout, Stderr: stderr,
		}
	}
	compilerRoot, cleanup, err := prepareCompilerRoot(compiledRoot)
	if err != nil {
		fmt.Fprintln(stderr, "harnest-runtime:", err)
		return 2
	}
	defer cleanup()
	compiler := engine.PythonCompiler{Python: python, OutputRoot: compilerRoot, Stderr: stderr}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := engine.CompileAndDeployAll(ctx, plan, compiler, deployer); err != nil {
		fmt.Fprintln(stderr, "harnest-runtime:", err)
		return 1
	}
	return 0
}

func prepareCompilerRoot(configured string) (string, func(), error) {
	if configured == "" {
		directory, err := os.MkdirTemp("", "harnest-compiled-")
		if err != nil {
			return "", func() {}, fmt.Errorf("create temporary compiler root: %w", err)
		}
		return directory, func() { _ = os.RemoveAll(directory) }, nil
	}
	absolute, err := filepath.Abs(configured)
	if err != nil {
		return "", func() {}, fmt.Errorf("resolve compiler root: %w", err)
	}
	if err := os.MkdirAll(absolute, 0o755); err != nil {
		return "", func() {}, fmt.Errorf("create compiler root %s: %w", absolute, err)
	}
	return absolute, func() {}, nil
}

func planReader(orchestratorPath, planPath, python string, stdin io.Reader, stderr io.Writer) (io.Reader, io.Closer, error) {
	if (orchestratorPath == "") == (planPath == "") {
		return nil, nil, fmt.Errorf("provide exactly one of -orchestrator or -plan")
	}
	if planPath == "-" {
		return stdin, nil, nil
	}
	if planPath != "" {
		file, err := os.Open(planPath)
		if err != nil {
			return nil, nil, fmt.Errorf("open deployment plan %s: %w", planPath, err)
		}
		return file, file, nil
	}
	if strings.TrimSpace(python) == "" {
		return nil, nil, fmt.Errorf("-python cannot be empty")
	}
	command := exec.Command(python, "-m", "harnest.cli", "plan", orchestratorPath)
	command.Stderr = stderr
	output, err := command.Output()
	if err != nil {
		return nil, nil, fmt.Errorf("render Python orchestrator: %w", err)
	}
	return strings.NewReader(string(output)), nil, nil
}
