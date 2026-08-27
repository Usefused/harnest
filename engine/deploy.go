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

	type deploymentJob struct {
		index  int
		bundle Bundle
	}
	jobs := make(chan deploymentJob)
	deploymentErrors := make([]error, len(bundles))
	var errorsMutex sync.Mutex
	var failFastTriggered atomic.Bool
	workerCount := min(plan.Parallelism, len(bundles))
	var workers sync.WaitGroup
	for range workerCount {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for job := range jobs {
				if ctx.Err() != nil || (plan.FailFast && failFastTriggered.Load()) {
					return
				}
				bundle := job.bundle
				if compiler != nil {
					artifact, err := compiler.Compile(ctx, bundle)
					if err != nil {
						errorsMutex.Lock()
						deploymentErrors[job.index] = fmt.Errorf("compile %s: %w", bundle.Config.Metadata.Name, err)
						errorsMutex.Unlock()
						if plan.FailFast {
							failFastTriggered.Store(true)
							cancel()
							return
						}
						continue
					}
					bundle.Compiled = &artifact
				}
				if err := deployer.Deploy(ctx, bundle); err != nil {
					errorsMutex.Lock()
					deploymentErrors[job.index] = fmt.Errorf("deploy %s: %w", job.bundle.Config.Metadata.Name, err)
					errorsMutex.Unlock()
					if plan.FailFast {
						failFastTriggered.Store(true)
						cancel()
						return
					}
				}
			}
		}()
	}

	for index, bundle := range bundles {
		if ctx.Err() != nil || (plan.FailFast && failFastTriggered.Load()) {
			break
		}
		select {
		case jobs <- deploymentJob{index: index, bundle: bundle}:
		case <-ctx.Done():
			break
		}
		if ctx.Err() != nil {
			break
		}
	}
	close(jobs)
	workers.Wait()
	joinedErrors := make([]error, 0, len(deploymentErrors)+1)
	for _, deploymentError := range deploymentErrors {
		if deploymentError != nil {
			joinedErrors = append(joinedErrors, deploymentError)
		}
	}
	if parentError := ctx.Err(); parentError != nil && !failFastTriggered.Load() {
		joinedErrors = append(joinedErrors, parentError)
	}
	return errors.Join(joinedErrors...)
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
