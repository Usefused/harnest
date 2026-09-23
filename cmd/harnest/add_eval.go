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
	var interactive bool
	command := &cobra.Command{
		Use:     "eval NAME",
		Short:   "Add an evaluation set or a bare custom metric",
		Example: "  harnest add eval answer-quality\n  harnest add eval answer-quality --i\n  harnest add eval company-quality --metric custom",
		Args:    cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			choice, err := selectEvalScaffold(command, metric, interactive)
			if err != nil {
				return err
			}
			path, err := addEvalResource(project, arguments[0], choice)
			if err != nil {
				return err
			}
			fmt.Fprintf(command.OutOrStdout(), "Added eval %q at %s\n", arguments[0], path)
			if choice.ID == "custom" {
				fmt.Fprintln(command.OutOrStdout(), "Next: implement the scorer, then register its harnest.lib import in evals/test_config.json customMetrics and criteria.")
				return nil
			}
			fmt.Fprintf(command.OutOrStdout(), "Scoring: %s (%s). Criteria in evals/test_config.json apply to all eval sets; existing settings are preserved.\n", choice.ID, choice.Backend)
			fmt.Fprintln(command.OutOrStdout(), "Next: edit the sample cases and criteria, configure the scoring backend, then run `harnest test . --evals` from the agent folder.")
			return nil
		},
	}
	flags := command.Flags()
	flags.StringVar(&project, "project", ".", "Harnest agent root containing config.yaml")
	flags.StringVar(&metric, "metric", "response_match_score", "built-in metric ID or custom; use --i to see choices")
	flags.BoolVarP(&interactive, "interactive", "i", false, "choose an evaluation interactively")
	flags.BoolVar(&interactive, "i", false, "alias for --interactive")
	return command
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
