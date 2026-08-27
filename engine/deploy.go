package engine

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
)

// Deployer is the narrow adapter your Go engine client implements.
// Bundle contents are immutable for the duration of a deployment run.
type Deployer interface {
	Deploy(context.Context, Bundle) error
}

type deploymentJob struct {
	index  int
	bundle Bundle
}

type deploymentRun struct {
	ctx               context.Context
	cancel            context.CancelFunc
	plan              DeploymentPlan
	compiler          Compiler
	deployer          Deployer
	errors            []error
	errorsMutex       sync.Mutex
	failFastTriggered atomic.Bool
}

// DeployAll deploys source bundles without compiling them. Deprecated: runtime
// integrations should use CompileAndDeployAll so deployers never import raw
// authored entrypoints.
func DeployAll(ctx context.Context, plan DeploymentPlan, deployer Deployer) error {
	return deployAll(ctx, plan, nil, deployer)
}

// CompileAndDeployAll compiles every discovered source bundle before handing it
// to the deployer. Compilation and deployment share the plan's concurrency and
// fail-fast policy.
func CompileAndDeployAll(ctx context.Context, plan DeploymentPlan, compiler Compiler, deployer Deployer) error {
	if compiler == nil {
		return fmt.Errorf("compiler is nil")
	}
	return deployAll(ctx, plan, compiler, deployer)
}

func deployAll(ctx context.Context, plan DeploymentPlan, compiler Compiler, deployer Deployer) error {
	if ctx == nil {
		return fmt.Errorf("deployment context is nil")
	}
	if deployer == nil {
		return fmt.Errorf("deployer is nil")
	}
	if err := plan.Validate(); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	bundles, err := Discover(plan)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	run := &deploymentRun{ctx: ctx, cancel: cancel, plan: plan, compiler: compiler, deployer: deployer, errors: make([]error, len(bundles))}
	jobs := make(chan deploymentJob)
	workerCount := min(plan.Parallelism, len(bundles))
	var workers sync.WaitGroup
	for range workerCount {
		workers.Add(1)
		go run.worker(jobs, &workers)
	}
	run.enqueue(jobs, bundles)
	close(jobs)
	workers.Wait()
	return run.joinErrors()
}

func (r *deploymentRun) worker(jobs <-chan deploymentJob, workers *sync.WaitGroup) {
	defer workers.Done()
	for job := range jobs {
		if r.stopped() {
			return
		}
		bundle, ok := r.compile(job)
		if !ok {
			if r.stopped() {
				return
			}
			continue
		}
		if err := r.deployer.Deploy(r.ctx, bundle); err != nil {
			r.record(job.index, "deploy", bundle, err)
			if r.stopped() {
				return
			}
		}
	}
}

func (r *deploymentRun) compile(job deploymentJob) (Bundle, bool) {
	if r.compiler == nil {
		return job.bundle, true
	}
	artifact, err := r.compiler.Compile(r.ctx, job.bundle)
	if err != nil {
		r.record(job.index, "compile", job.bundle, err)
		return Bundle{}, false
	}
	job.bundle.Compiled = &artifact
	return job.bundle, true
}

func (r *deploymentRun) record(index int, operation string, bundle Bundle, err error) {
	r.errorsMutex.Lock()
	r.errors[index] = fmt.Errorf("%s %s: %w", operation, bundle.Config.Metadata.Name, err)
	r.errorsMutex.Unlock()
	if r.plan.FailFast {
		r.failFastTriggered.Store(true)
		r.cancel()
	}
}

func (r *deploymentRun) stopped() bool {
	return r.ctx.Err() != nil || (r.plan.FailFast && r.failFastTriggered.Load())
}

func (r *deploymentRun) enqueue(jobs chan<- deploymentJob, bundles []Bundle) {
	for index, bundle := range bundles {
		if r.stopped() {
			return
		}
		select {
		case jobs <- deploymentJob{index: index, bundle: bundle}:
		case <-r.ctx.Done():
			return
		}
	}
}

func (r *deploymentRun) joinErrors() error {
	joined := make([]error, 0, len(r.errors)+1)
	for _, err := range r.errors {
		if err != nil {
			joined = append(joined, err)
		}
	}
	if err := r.ctx.Err(); err != nil && !r.failFastTriggered.Load() {
		joined = append(joined, err)
	}
	return errors.Join(joined...)
}

// CommandDeployer is a production-friendly bridge while the engine client is
// being developed. It invokes a binary directly (never through a shell) and
// appends the compiled artifact directory to the configured arguments.
type CommandDeployer struct {
	Command string
	Args    []string
	Stdout  io.Writer
	Stderr  io.Writer
}

func (d CommandDeployer) Deploy(ctx context.Context, bundle Bundle) error {
	if ctx == nil {
		return fmt.Errorf("deployment context is nil")
	}
	if strings.TrimSpace(d.Command) == "" {
		return fmt.Errorf("deployment command is empty")
	}
	deploymentDirectory := bundle.Directory
	if bundle.Compiled != nil {
		deploymentDirectory = bundle.Compiled.Directory
	}
	args := append(append([]string{}, d.Args...), deploymentDirectory)
	command := exec.CommandContext(ctx, d.Command, args...)
	command.Stdout = d.Stdout
	command.Stderr = d.Stderr
	labels, err := json.Marshal(bundle.Labels)
	if err != nil {
		return fmt.Errorf("encode deployment labels: %w", err)
	}
	command.Env = append(os.Environ(),
		"HARNEST_AGENT_NAME="+bundle.Config.Metadata.Name,
		"HARNEST_AGENT_DIGEST="+bundle.Digest,
		"HARNEST_AGENT_CONFIG="+bundle.ConfigPath,
		"HARNEST_AGENT_CARD="+bundle.CardPath,
		"HARNEST_AGENT_INSTRUCTIONS="+bundle.InstructionsPath,
		"HARNEST_AGENT_LABELS="+string(labels),
	)
	if bundle.Compiled != nil {
		command.Env = append(command.Env,
			"HARNEST_AGENT_SOURCE="+bundle.Directory,
			"HARNEST_COMPILED_DIRECTORY="+bundle.Compiled.Directory,
			"HARNEST_COMPILED_MANIFEST="+bundle.Compiled.ManifestPath,
			"HARNEST_COMPILED_ENTRYPOINT="+bundle.Compiled.Manifest.Entrypoint,
			"HARNEST_COMPILED_DIGEST="+bundle.Compiled.Manifest.Digest,
		)
	}
	return command.Run()
}

type DryRunDeployer struct {
	Writer io.Writer
}

func (d DryRunDeployer) Deploy(_ context.Context, bundle Bundle) error {
	if d.Writer == nil {
		return fmt.Errorf("dry-run writer is nil")
	}
	if bundle.Compiled != nil {
		_, err := fmt.Fprintf(d.Writer, "deploy %s (%s) from compiled artifact %s using %s\n",
			bundle.Config.Metadata.Name, bundle.Compiled.Manifest.Digest, bundle.Compiled.Directory, bundle.Compiled.Manifest.Entrypoint)
		return err
	}
	_, err := fmt.Fprintf(d.Writer, "deploy %s (%s) from source %s\n", bundle.Config.Metadata.Name, bundle.Digest, bundle.Directory)
	return err
}
