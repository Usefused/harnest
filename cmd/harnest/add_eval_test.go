package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestAddEvalCreatesPresets covers every CLI choice without initializing Python dependencies.
func TestAddEvalCreatesPresets(t *testing.T) {
	choices, err := evalScaffolds()
	if err != nil {
		t.Fatal(err)
	}
	for _, choice := range choices {
		t.Run(choice.ID, func(t *testing.T) {
			root := filepath.Join(t.TempDir(), "agent")
			if _, _, err := executeForTest(t, defaultSystem(), "init", root, "--minimal"); err != nil {
				t.Fatal(err)
			}
			output, _, err := executeForTest(t, defaultSystem(), "add", "eval", "my-eval", "--project", root, "--metric", choice.ID)
			if err != nil {
				t.Fatal(err)
			}
			if choice.ID == "custom" {
				source := string(mustReadTestFile(t, filepath.Join(root, "lib", "my_eval.py")))
				assertContainsAll(t, "custom scorer", source, []string{"@metric", "async def my_eval", "NotImplementedError"})
				if _, err := os.Stat(filepath.Join(root, "evals")); !os.IsNotExist(err) {
					t.Fatalf("custom wrote evals: %v", err)
				}
				return
			}
			assertBuiltinEvalScaffold(t, root, output, choice)
		})
	}
}

// assertBuiltinEvalScaffold checks the file contract without requiring a model or dependency setup.
func assertBuiltinEvalScaffold(t *testing.T, root, output string, choice evalScaffold) {
	t.Helper()
	assertContainsAll(t, "eval output", output, []string{choice.ID, choice.Backend, "all eval sets"})
	path := filepath.Join(root, "evals", "my_eval.evalset.json")
	var set map[string]any
	if err := json.Unmarshal(mustReadTestFile(t, path), &set); err != nil {
		t.Fatal(err)
	}
	if set["eval_set_id"] != "my_eval" {
		t.Fatal(set)
	}
	var config map[string]any
	if err := json.Unmarshal(mustReadTestFile(t, filepath.Join(root, "evals", "test_config.json")), &config); err != nil {
		t.Fatal(err)
	}
	if config["criteria"].(map[string]any)[choice.ID] == nil {
		t.Fatal(config)
	}
	if _, err := os.Stat(filepath.Join(root, ".harnest", "environments")); !os.IsNotExist(err) {
		t.Fatalf("scaffolding created environment: %v", err)
	}
}

// TestAddEvalInteractiveAliases exercises real command input, cancellation, and script conflicts.
func TestAddEvalInteractiveAliases(t *testing.T) {
	for _, flag := range []string{"--i", "-i", "--interactive"} {
		root := filepath.Join(t.TempDir(), "agent")
		if err := createScaffold(root, "agent"); err != nil {
			t.Fatal(err)
		}
		command := newRootCommand(defaultSystem(), "test-version")
		var output bytes.Buffer
		command.SetOut(&output)
		command.SetErr(&output)
		command.SetIn(strings.NewReader("14\n"))
		command.SetArgs([]string{"add", "eval", "quality", flag, "--project", root})
		if err := command.ExecuteContext(context.Background()); err != nil {
			t.Fatal(err)
		}
		assertContainsAll(t, "picker", output.String(), []string{"Response text similarity", "Vertex evaluation service", "Custom metric"})
		assertFilesExist(t, root, []string{"lib/quality.py"})
	}
	for _, input := range []string{"", "q\n", "99\n", "made-up\n"} {
		root := filepath.Join(t.TempDir(), "agent")
		if _, _, err := executeForTest(t, defaultSystem(), "init", root, "--minimal"); err != nil {
			t.Fatal(err)
		}
		command := newRootCommand(defaultSystem(), "test-version")
		command.SetOut(&bytes.Buffer{})
		command.SetErr(&bytes.Buffer{})
		command.SetIn(strings.NewReader(input))
		command.SetArgs([]string{"add", "eval", "quality", "--i", "--project", root})
		if err := command.Execute(); err == nil {
			t.Fatalf("accepted input %q", input)
		}
		if _, err := os.Stat(filepath.Join(root, "evals")); !os.IsNotExist(err) {
			t.Fatalf("cancelled command wrote files: %v", err)
		}
	}
	if _, _, err := executeForTest(t, defaultSystem(), "add", "eval", "quality", "--i", "--metric", "custom"); err == nil {
		t.Fatal("accepted conflicting options")
	}
}

// TestAddEvalPreservesConfigAndRejectsCollisions protects authored scores, custom metrics, and files.
func TestAddEvalPreservesConfigAndRejectsCollisions(t *testing.T) {
	root := filepath.Join(t.TempDir(), "agent")
	if err := createScaffold(root, "agent"); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(root, "evals", "test_config.json")
	original := `{"criteria":{"response_match_score":0.91},"customMetrics":{"company":{"codeConfig":{"name":"harnest.lib.company.score"}}},"customField":{"keep":true}}`
	if err := os.WriteFile(configPath, []byte(original), 0o640); err != nil {
		t.Fatal(err)
	}
	if _, _, err := executeForTest(t, defaultSystem(), "add", "eval", "first", "--project", root); err != nil {
		t.Fatal(err)
	}
	if string(mustReadTestFile(t, configPath)) != original {
		t.Fatal("rewrote existing criterion")
	}
	if _, _, err := executeForTest(t, defaultSystem(), "add", "eval", "second", "--project", root, "--metric", "safety_v1"); err != nil {
		t.Fatal(err)
	}
	after := mustReadTestFile(t, configPath)
	assertContainsAll(t, "merged config", string(after), []string{"0.91", "harnest.lib.company.score", "customField", "safety_v1"})
	for _, name := range []string{"second", "../outside", "class"} {
		if _, _, err := executeForTest(t, defaultSystem(), "add", "eval", name, "--project", root); err == nil {
			t.Fatalf("accepted collision or invalid name %q", name)
		}
		if !bytes.Equal(after, mustReadTestFile(t, configPath)) {
			t.Fatal("failed command changed config")
		}
	}
}

// TestAddEvalRejectsInvalidConfig leaves malformed configuration untouched.
func TestAddEvalRejectsInvalidConfig(t *testing.T) {
	for _, content := range []string{"not json", "null", "[]", `{"criteria":null}`, `{"criteria":[]}`} {
		root := filepath.Join(t.TempDir(), "agent")
		if err := createScaffold(root, "agent"); err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(root, "evals", "test_config.json")
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
		if _, _, err := executeForTest(t, defaultSystem(), "add", "eval", "quality", "--project", root); err == nil {
			t.Fatalf("accepted %q", content)
		}
		if _, err := os.Stat(filepath.Join(root, "evals", "quality.evalset.json")); !os.IsNotExist(err) {
			t.Fatalf("left partial eval: %v", err)
		}
	}
}

// TestAddEvalRejectsConfigLinks prevents edits outside the project.
func TestAddEvalRejectsConfigLinks(t *testing.T) {
	root := filepath.Join(t.TempDir(), "agent")
	if err := createScaffold(root, "agent"); err != nil {
		t.Fatal(err)
	}
	external := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(external, []byte(`{"criteria":{}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "evals", "test_config.json")
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	if err := os.Symlink(external, path); err != nil {
		t.Fatal(err)
	}
	if _, _, err := executeForTest(t, defaultSystem(), "add", "eval", "quality", "--project", root); err == nil {
		t.Fatal("accepted config symlink")
	}
	if string(mustReadTestFile(t, external)) != `{"criteria":{}}` {
		t.Fatal("changed external config")
	}
}
