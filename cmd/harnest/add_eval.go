// `harnest add eval NAME`: chooses an eval scaffold, by flag or prompt, and writes it.

package main

import (
	"bufio"
	_ "embed"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
)

//go:embed eval_scaffolds.json
var evalScaffoldCatalog []byte

type evalScaffold struct {
	ID        string          `json:"id"`
	Label     string          `json:"label"`
	Backend   string          `json:"backend"`
	Criterion json.RawMessage `json:"criterion"`
	Scenario  bool            `json:"scenario"`
	MultiTurn bool            `json:"multiTurn"`
}

// evalScaffolds loads authoring presets, not a runtime metric allowlist.
func evalScaffolds() ([]evalScaffold, error) {
	var choices []evalScaffold
	err := json.Unmarshal(evalScaffoldCatalog, &choices)
	return choices, err
}

// newAddEvalCommand scaffolds evaluations without resolving or running dependencies.
func (a *application) newAddEvalCommand() *cobra.Command {
	var project, metric string
	var interactive, listMetrics, scoped bool
	command := &cobra.Command{
		Use:     "eval NAME",
		Short:   "Add an evaluation set or a bare custom metric",
		Example: "  harnest add eval answer-quality\n  harnest add eval tool-calls --metric tool_trajectory_avg_score --scoped\n  harnest add eval answer-quality --i\n  harnest add eval company-quality --metric custom",
		Args: func(command *cobra.Command, arguments []string) error {
			if listMetrics {
				return cobra.NoArgs(command, arguments)
			}
			return cobra.ExactArgs(1)(command, arguments)
		},
		RunE: func(command *cobra.Command, arguments []string) error {
			if listMetrics {
				return writeEvalScaffolds(command)
			}
			choice, err := selectEvalScaffold(command, metric, interactive)
			if err != nil {
				return err
			}
			// Custom metrics are Python modules registered in a config file, not eval sets.
			if scoped && choice.ID == "custom" {
				return fmt.Errorf("--scoped applies to built-in eval sets; register custom metrics in evals/test_config.json or <set>.test_config.json")
			}
			path, err := addEvalResource(project, arguments[0], choice, scoped)
			if err != nil {
				return err
			}
			writeAddEvalSummary(command, arguments[0], path, choice, scoped)
			return nil
		},
	}
	flags := command.Flags()
	flags.StringVar(&project, "project", ".", "Harnest agent root containing config.yaml")
	flags.StringVar(&metric, "metric", "response_match_score", "built-in metric ID or custom; use --i to see choices")
	flags.BoolVar(&scoped, "scoped", false, "write criteria to <set>.test_config.json instead of shared evals/test_config.json")
	flags.BoolVarP(&interactive, "interactive", "i", false, "choose an evaluation interactively")
	flags.BoolVar(&interactive, "i", false, "alias for --interactive")
	flags.BoolVar(&listMetrics, "list-metrics", false, "print evaluation presets as JSON without creating files")
	return command
}

// writeAddEvalSummary reports the created eval files and where criteria were recorded.
func writeAddEvalSummary(command *cobra.Command, requested, path string, choice evalScaffold, scoped bool) {
	fmt.Fprintf(command.OutOrStdout(), "Added eval %q at %s\n", requested, path)
	if choice.ID == "custom" {
		fmt.Fprintln(command.OutOrStdout(), "Next: implement the scorer, then register its harnest.lib import in evals/test_config.json customMetrics and criteria.")
		return
	}
	// Report scoped vs shared config clearly so authors know which sets share criteria.
	if scoped {
		name, _ := normalizedAgentResourceName(requested)
		fmt.Fprintf(command.OutOrStdout(), "Scoring: %s (%s) scoped to evals/%s.test_config.json.\n", choice.ID, choice.Backend, name)
	} else {
		fmt.Fprintf(command.OutOrStdout(), "Scoring: %s (%s). Shared criteria in evals/test_config.json apply unless a set has its own <set>.test_config.json (use --scoped to create one).\n", choice.ID, choice.Backend)
	}
	fmt.Fprintln(command.OutOrStdout(), "Next: edit the sample cases and criteria, configure the scoring backend, then run `harnest test . --evals` from the agent folder.")
}

// writeEvalScaffolds exposes the same presets to Studio without project execution.
func writeEvalScaffolds(command *cobra.Command) error {
	choices, err := evalScaffolds()
	if err != nil {
		return err
	}
	return json.NewEncoder(command.OutOrStdout()).Encode(choices)
}

// selectEvalScaffold shares the catalog between scripted and interactive creation.
func selectEvalScaffold(command *cobra.Command, metric string, interactive bool) (evalScaffold, error) {
	choices, err := evalScaffolds()
	if err != nil {
		return evalScaffold{}, err
	}
	if interactive {
		if command.Flags().Changed("metric") {
			return evalScaffold{}, fmt.Errorf("choose either --metric or --i")
		}
		metric, err = promptEvalScaffold(command, choices)
		if err != nil {
			return evalScaffold{}, err
		}
	}
	for _, choice := range choices {
		if choice.ID == metric {
			return choice, nil
		}
	}
	return evalScaffold{}, fmt.Errorf("unknown evaluation metric %q; use --i to see supported presets", metric)
}

// promptEvalScaffold accepts a displayed number or metric ID and treats EOF as cancellation.
func promptEvalScaffold(command *cobra.Command, choices []evalScaffold) (string, error) {
	output := command.OutOrStdout()
	fmt.Fprintln(output, "Choose an evaluation:")
	for index, choice := range choices {
		fmt.Fprintf(output, "  %d. %s — %s [%s]\n", index+1, choice.Label, choice.ID, choice.Backend)
	}
	fmt.Fprint(output, "Selection (number or metric ID; q to cancel): ")
	scanner := bufio.NewScanner(command.InOrStdin())
	if !scanner.Scan() {
		if err := scanner.Err(); err != nil {
			return "", err
		}
		return "", fmt.Errorf("evaluation selection cancelled")
	}
	value := strings.TrimSpace(scanner.Text())
	if value == "" || value == "q" {
		return "", fmt.Errorf("evaluation selection cancelled")
	}
	if index, err := strconv.Atoi(value); err == nil && index > 0 && index <= len(choices) {
		return choices[index-1].ID, nil
	}
	return value, nil
}
