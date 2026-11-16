// Agent evaluation settings (spec.evaluation) and their validation.

package engine

import "fmt"

// BuiltinEvaluationRunner is the spec.evaluation.runners name of the agent
// framework's built-in evaluation engine, and the runner used when the section
// is absent.
const BuiltinEvaluationRunner = "adk"

// AgentEvaluation selects ordered evaluators without importing their Python packages.
type AgentEvaluation struct {
	Runners []EvaluationRunner `yaml:"runners" json:"runners"`
}

type EvaluationRunner struct {
	Name       string         `yaml:"name" json:"name"`
	Entrypoint string         `yaml:"entrypoint,omitempty" json:"entrypoint,omitempty"`
	Options    map[string]any `yaml:"options,omitempty" json:"options,omitempty"`
}

// UsesBuiltinRunner reports whether the framework's built-in evaluation engine runs,
// including configurations written before runner selection.
func (e *AgentEvaluation) UsesBuiltinRunner() bool {
	if e == nil {
		return true
	}
	for _, runner := range e.Runners {
		if runner.Name == BuiltinEvaluationRunner {
			return true
		}
	}
	return false
}

// UsesADK is the earlier name of UsesBuiltinRunner.
//
// Deprecated: use UsesBuiltinRunner.
func (e *AgentEvaluation) UsesADK() bool {
	return e.UsesBuiltinRunner()
}

// validate rejects ambiguous registrations before resolving the evaluation environment.
func (e *AgentEvaluation) validate() error {
	if e == nil {
		return nil
	}
	if len(e.Runners) == 0 {
		return fmt.Errorf("spec.evaluation.runners must not be empty")
	}
	seen := map[string]bool{}
	for _, runner := range e.Runners {
		if !agentNamePattern.MatchString(runner.Name) || seen[runner.Name] {
			return fmt.Errorf("invalid or duplicate evaluation runner name %q", runner.Name)
		}
		seen[runner.Name] = true
		if err := runner.validate(); err != nil {
			return err
		}
	}
	return nil
}

// validate keeps the built-in runner's existing eval-set configuration authoritative.
func (r EvaluationRunner) validate() error {
	if r.Name == BuiltinEvaluationRunner {
		if r.Entrypoint != "" || r.Options != nil {
			return fmt.Errorf("the %s evaluation runner accepts only name", BuiltinEvaluationRunner)
		}
	} else if !entrypointPattern.MatchString(r.Entrypoint) {
		return fmt.Errorf("evaluation runner %q requires a module:function entrypoint", r.Name)
	}
	return nil
}
