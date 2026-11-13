package main

import (
	"path/filepath"
	"strings"
	"testing"

	"harnest.dev/harnest/engine"
)

// TestRunnerSelectionLocksOnlyEvalDependencies checks plain, mixed, and replacement profiles against one project.
func TestRunnerSelectionLocksOnlyEvalDependencies(t *testing.T) {
	root, agent := scaffoldIDEEnvironmentTestAgent(t)
	calls := filepath.Join(root, "uv-calls")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	sys := environmentTestSystem(t, root)
	project := filepath.Join(agent, "pyproject.toml")
	contents := string(mustReadTestFile(t, project))
	mustWriteEnvironmentFixture(t, project, strings.Replace(contents, "[dependency-groups]", "[dependency-groups]\neval = ['company-evals==1.0.0']", 1))
	config := filepath.Join(agent, "config.yaml")
	original := string(mustReadTestFile(t, config))
	company := "    runners:\n      - name: company\n        entrypoint: company_evals:run\n"
	update := func(runners string) {
		t.Helper()
		mustWriteEnvironmentFixture(t, config, strings.Replace(original, "spec:\n", "spec:\n  evaluation:\n"+runners, 1))
	}
	update(company)
	mustSyncAgentEnvironment(t, sys, agent)
	runtimeCalls := string(mustReadTestFile(t, calls))
	if strings.Contains(runtimeCalls, "--group") {
		t.Fatal("serving installed the evaluation group")
	}
	mustSyncAgentEnvironment(t, sys, agent, "--profile", "eval")
	logs := string(mustReadTestFile(t, calls))
	assertContainsAll(t, "external eval profile", logs[len(runtimeCalls):], []string{"--group ", "pyproject.toml:eval", "harnest[adk,test]"})
	update(company + "      - name: adk\n")
	if _, _, err := executeForTest(t, sys, "env", "sync", agent, "--profile", "eval", "--frozen"); err == nil || !strings.Contains(err.Error(), "stale") {
		t.Fatalf("old custom-only lock should be stale after adding ADK: %v", err)
	}
	mustSyncAgentEnvironment(t, sys, agent, "--profile", "eval")
	assertContainsAll(t, "mixed profile", string(mustReadTestFile(t, calls))[len(logs):], []string{"--group ", "harnest[adk,eval]"})
	update(company + "      - name: company\n        entrypoint: other:run\n")
	if _, err := engine.LoadBundle(agent); err == nil {
		t.Fatal("accepted duplicate runner name")
	}
}
