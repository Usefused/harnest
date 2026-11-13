// Agent evaluation settings (spec.evaluation) and their validation.

package engine

import "fmt"

// AgentEvaluation selects ordered evaluators without importing their Python packages.
type AgentEvaluation struct {
	Runners []EvaluationRunner `yaml:"runners" json:"runners"`
}

type EvaluationRunner struct {
	Name       string         `yaml:"name" json:"name"`
	Entrypoint string         `yaml:"entrypoint,omitempty" json:"entrypoint,omitempty"`
	Options    map[string]any `yaml:"options,omitempty" json:"options,omitempty"`
}

// UsesADK preserves the default evaluator for configurations written before runner selection.
func (e *AgentEvaluation) UsesADK() bool {
	if e == nil {
		return true
	}
	for _, runner := range e.Runners {
		if runner.Name == "adk" {
			return true
		}
	}
	return false
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
	if r.Name == "adk" {
		if r.Entrypoint != "" || r.Options != nil {
			return fmt.Errorf("the adk evaluation runner accepts only name")
		}
	} else if !entrypointPattern.MatchString(r.Entrypoint) {
		return fmt.Errorf("evaluation runner %q requires a module:function entrypoint", r.Name)
	}
	return nil
}
