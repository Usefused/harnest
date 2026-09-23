package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// addEvalResource applies the same root, naming, and no-overwrite rules as other resources.
func addEvalResource(project, requested string, choice evalScaffold) (string, error) {
	if choice.ID == "custom" {
		path, _, err := addAgentResource(project, requested, agentResourceScaffold{
			Kind: "eval", Directory: "lib", Source: customEvalSource,
		})
		return path, err
	}
	root, err := validatedAgentProject(project)
	if err != nil {
		return "", err
	}
	name, err := normalizedAgentResourceName(requested)
	if err != nil {
		return "", err
	}
	if err := validateAgentResourceCompatibility(root, agentResourceScaffold{Kind: "eval"}); err != nil {
		return "", err
	}
	return createEvalFiles(root, name, choice)
}

// createEvalFiles validates configuration before writing and rolls back a failed paired update.
func createEvalFiles(root, name string, choice evalScaffold) (path string, returnErr error) {
	directory := filepath.Join(root, "evals")
	madeDirectory, err := prepareAgentResourceDirectory(directory, "evals")
	if err != nil {
		return "", err
	}
	defer func() {
		if returnErr != nil && madeDirectory {
			_ = os.Remove(directory)
		}
	}()
	configPath := filepath.Join(directory, "test_config.json")
	previous, updated, err := evalConfigUpdate(configPath, choice)
	if err != nil {
		return "", err
	}
	contents, err := json.MarshalIndent(evalSetSource(name, choice), "", "  ")
	if err != nil {
		return "", err
	}
	path = filepath.Join(directory, name+".evalset.json")
	if err := createScaffoldFile(path, string(contents)+"\n"); err != nil {
		return "", err
	}
	if err := publishEvalConfig(configPath, previous, updated); err != nil {
		_ = os.Remove(path)
		return "", err
	}
	return path, nil
}

// evalConfigUpdate preserves all authored settings and never replaces an existing threshold.
func evalConfigUpdate(path string, choice evalScaffold) ([]byte, []byte, error) {
	previous, err := readRegularDependencyFile(path)
	if err != nil && !os.IsNotExist(err) {
		return nil, nil, fmt.Errorf("read eval config: %w", err)
	}
	config := map[string]json.RawMessage{}
	if err == nil {
		if err := json.Unmarshal(previous, &config); err != nil {
			return nil, nil, fmt.Errorf("invalid eval config: %w", err)
		}
	}
	if config == nil {
		return nil, nil, fmt.Errorf("eval config must be a JSON object")
	}
	changed, err := addEvalCriterion(config, choice)
	if err != nil {
		return nil, nil, err
	}
	if !changed {
		return previous, previous, nil
	}
	updated, err := json.MarshalIndent(config, "", "  ")
	return previous, append(updated, '\n'), err
}

// addEvalCriterion adds only absent keys, including bounded simulator defaults when needed.
func addEvalCriterion(config map[string]json.RawMessage, choice evalScaffold) (bool, error) {
	criteria := map[string]json.RawMessage{}
	if value, ok := config["criteria"]; ok {
		if err := json.Unmarshal(value, &criteria); err != nil {
			return false, fmt.Errorf("eval criteria must be an object: %w", err)
		}
	}
	if criteria == nil {
		return false, fmt.Errorf("eval criteria must be a JSON object")
	}
	changed := false
	if _, exists := criteria[choice.ID]; !exists {
		criteria[choice.ID] = choice.Criterion
		encoded, err := json.Marshal(criteria)
		if err != nil {
			return false, err
		}
		config["criteria"] = encoded
		changed = true
	}
	_, camel := config["userSimulatorConfig"]
	_, snake := config["user_simulator_config"]
	if choice.Scenario && !camel && !snake {
		config["userSimulatorConfig"] = json.RawMessage(`{"type":"llm_backed","maxAllowedInvocations":4}`)
		changed = true
	}
	return changed, nil
}

// publishEvalConfig avoids overwrites on creation and detects edits made while scaffolding.
func publishEvalConfig(path string, previous, updated []byte) error {
	if bytes.Equal(previous, updated) {
		return nil
	}
	if previous == nil {
		return createScaffoldFile(path, string(updated))
	}
	current, err := readRegularDependencyFile(path)
	if err != nil {
		return err
	}
	if !bytes.Equal(current, previous) {
		return fmt.Errorf("eval config changed while scaffolding; retry")
	}
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	return replaceRegularFileMode(path, updated, info.Mode().Perm())
}

// customEvalSource leaves scoring and registration to the author, with no passing placeholder.
func customEvalSource(name string) string {
	return fmt.Sprintf(`from harnest.evaluation import MetricContext, MetricScore, metric


@metric
async def %s(context: MetricContext) -> MetricScore:
    """Score one conversation using your own rules or service."""
    raise NotImplementedError("Implement this metric before registering it")
`, name)
}

// evalSetSource chooses evidence appropriate to static, multi-turn, or simulated metrics.
func evalSetSource(name string, choice evalScaffold) map[string]any {
	example := map[string]any{"evalId": name + "_case"}
	if choice.Scenario {
		example["conversationScenario"] = map[string]any{
			"startingPrompt":   "What is the capital of France?",
			"conversationPlan": "Ask for the capital of France, then ask for one landmark there. End when both questions are answered.",
		}
	} else {
		turns := []any{evalTurn("What is the capital of France?", "Paris is the capital of France.")}
		if choice.ID == "tool_trajectory_avg_score" {
			turns[0].(map[string]any)["intermediateData"] = map[string]any{
				"toolUses":      []any{map[string]any{"name": "replace_with_your_tool", "args": map[string]any{}}},
				"toolResponses": []any{},
			}
		}
		if choice.MultiTurn {
			turns = append(turns, evalTurn("Name one landmark there.", "The Eiffel Tower is in Paris."))
		}
		example["conversation"] = turns
	}
	return map[string]any{"eval_set_id": name, "name": name, "eval_cases": []any{example}}
}

// evalTurn emits native ADK content fields accepted by either framework's evaluator.
func evalTurn(prompt, answer string) map[string]any {
	return map[string]any{
		"userContent":   map[string]any{"role": "user", "parts": []any{map[string]any{"text": prompt}}},
		"finalResponse": map[string]any{"role": "model", "parts": []any{map[string]any{"text": answer}}},
	}
}
