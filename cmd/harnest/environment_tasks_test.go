package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const procrastinateRequirement = "procrastinate==3.9.0"

// TestTaskProvidersDoNotInjectQueueDependencies exercises complete resolver
// inputs for every profile without importing config-dependent Python factories.
func TestTaskProvidersDoNotInjectQueueDependencies(t *testing.T) {
	for _, provider := range []string{"harnest_postgres", "harnest_redis", "my_storage", "none"} {
		for _, profile := range environmentProfiles {
			t.Run(provider+"/"+string(profile), func(t *testing.T) {
				root := t.TempDir()
				agent := taskDependencyFixture(t, root, provider)
				calls := filepath.Join(root, "calls.txt")
				t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
				_, _, err := executeForTest(t, environmentTestSystem(t, root), "env", "sync", agent, "--profile", string(profile))
				if err != nil {
					t.Fatal(err)
				}
				contents := string(mustReadTestFile(t, calls))
				if strings.Contains(contents, "procrastinate") {
					t.Fatal("task declaration injected a queue library into the resolver input")
				}
				assertContainsAll(t, "environment setup", contents, []string{"pip compile", "pip sync", "--require-hashes"})
			})
		}
	}
}

// TestCustomStorageDependencyInvalidatesLocks preserves the ordinary
// dependency path for custom storage adapters and prevents frozen locks hiding a change.
func TestCustomStorageDependencyInvalidatesLocks(t *testing.T) {
	root := t.TempDir()
	agent := taskDependencyFixture(t, root, "none")
	calls := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	sys := environmentTestSystem(t, root)
	mustSyncAgentEnvironment(t, sys, agent)
	before := string(mustReadTestFile(t, calls))
	project := filepath.Join(agent, "pyproject.toml")
	mustWriteEnvironmentFixture(t, project, "[project]\nname = 'custom-agent'\nversion = '0.1.0'\ndependencies = ['custom-storage==1.0.0']\n")
	_, _, err := executeForTest(t, sys, "env", "sync", agent, "--frozen")
	if err == nil {
		t.Fatal("frozen lock accepted a new storage adapter dependency")
	}
	if string(mustReadTestFile(t, calls)) != before {
		t.Fatal("frozen validation ran a dependency resolver")
	}
	mustSyncAgentEnvironment(t, sys, agent)
	bundle, err := loadAgentBundle(agent)
	if err != nil {
		t.Fatal(err)
	}
	plan, err := inspectRuntimeDependencyPlan(bundle)
	if err != nil {
		t.Fatal(err)
	}
	requirements, err := projectRuntimeRequirements(plan.ProjectFiles[0], "agent")
	if err != nil || !containsString(requirements, "custom-storage==1.0.0") {
		t.Fatalf("storage dependency was not retained in the resolver project: %v, %v", requirements, err)
	}
	if string(mustReadTestFile(t, calls)) == before {
		t.Fatal("explicit storage dependency did not refresh the environment")
	}
}

// taskDependencyFixture leaves provider factories unexecuted until compilation;
// a custom adapter may not even be installed before environment synchronization.
func taskDependencyFixture(t *testing.T, root, provider string) string {
	t.Helper()
	agent := filepath.Join(root, "task-agent")
	if err := createScaffold(agent, "task-agent"); err != nil {
		t.Fatal(err)
	}
	for _, directory := range []string{"tasks", "lifecycle"} {
		if err := os.MkdirAll(filepath.Join(agent, directory), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	mustWriteEnvironmentFixture(t, filepath.Join(agent, "tasks", "deliver.py"), "from harnest.task import task\n@task\ndef deliver():\n    '''Return completed work.'''\n    return True\n")
	if provider != "none" {
		mustWriteEnvironmentFixture(t, filepath.Join(agent, "lifecycle", "queue.py"), "from harnest import lifecycle\nimport "+provider+"\n@lifecycle.storage.tasks\ndef storage():\n    raise AssertionError('environment sync must not evaluate storage factories')\n")
	}
	return agent
}
