package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"harnest.dev/harnest/engine"
)

func TestRootHelpTeachesStandaloneFilesystemWorkflow(t *testing.T) {
	stdout, _, err := executeForTest(t, defaultSystem(), "help")
	if err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{"harnest skills install", "harnest extensions search", "harnest init", "--example", "harnest env sync", "harnest mode advanced", "harnest upgrade", "--apply", "harnest test", "--eval-trajectory strict", "--eval-output eval-result.json", "harnest compile", "harnest run", "harnest serve", "harnest serve my-agent --reload", "config.yaml", "pyproject.toml", "lib/", "models/", "tools/", "tasks/", "cron/", "evals/"} {
		if !strings.Contains(stdout, expected) {
			t.Fatalf("help is missing %q:\n%s", expected, stdout)
		}
	}
	if strings.Contains(stdout, "plan") {
		t.Fatalf("standalone help unexpectedly mentions orchestrator plans:\n%s", stdout)
	}
}

// TestInitCreatesMinimalLoadableKebabNamedLiteLLMAgent checks runnable defaults without extra server YAML.
func TestInitCreatesMinimalLoadableKebabNamedLiteLLMAgent(t *testing.T) {
	target := filepath.Join(t.TempDir(), "support-agent")
	stdout, _, err := executeForTest(t, defaultSystem(), "init", target)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout, "support-agent") {
		t.Fatalf("unexpected init output %q", stdout)
	}
	bundle, err := engine.LoadBundle(target)
	if err != nil {
		t.Fatalf("generated bundle does not satisfy the Go contract: %v", err)
	}
	if bundle.Config.Metadata.Name != "support-agent" {
		t.Fatalf("got deployment name %q", bundle.Config.Metadata.Name)
	}
	assertScaffoldModelEnvironment(t, bundle)
	agentSource, err := os.ReadFile(filepath.Join(target, "agent.py"))
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "generated agent.py", string(agentSource), []string{
		`name="support_agent"`,
		`history="session"`,
		"from harnest.agent import Agent",
		"root_agent = Agent(",
		"model=LiteLLMModel.from_openai_environment(),",
	})
	if strings.Contains(string(agentSource), "Graph(") {
		t.Fatalf("minimal scaffold unexpectedly contains a graph example:\n%s", agentSource)
	}
	assertDirectories(t, target, []string{
		"lib", "models", "tools", "tasks", "cron", "subagents", "mcp", "extensions", "plugins", "sandbox", "skills", "evals",
		"tests/unit", "tests/smoke",
	})
	assertFilesContain(t, target, map[string]string{
		"harnest.lock":           "projectSchema: 3",
		"pyproject.toml":         `[tool.uv]`,
		"lib/_README.md":         "from harnest.lib.audit import record_change",
		"models/_README.md":      "from harnest.models.support import",
		"lifecycle/storage.py":   "@lifecycle.storage.checkpoints",
		"tools/_README.md":       "Add one @tool callable",
		"tasks/_README.md":       "durable @task callable",
		"cron/_README.md":        "UTC Cron declaration",
		"plugins/_README.md":     "Agent Plugin folders",
		"lifecycle/_README.md":   "@lifecycle-decorated hooks",
		"extensions/_README.md":  "extension.yaml and extension.py",
		"tests/unit/_README.md":  "offline test_*.py",
		"tests/smoke/_README.md": "opt-in test_*.py",
	})
	// Safe defaults require no authored server file or empty override block.
	if _, err := os.Stat(filepath.Join(target, "server.yaml")); !os.IsNotExist(err) {
		t.Fatalf("minimal scaffold should omit server.yaml: %v", err)
	}
	if bundle.Config.Server != nil {
		t.Fatal("minimal scaffold should rely on server defaults")
	}
	assertOnlyPlaceholderResources(t, target)
}

// TestInitExampleFillsOnlyPlaceholderFolders checks the same inert profile on
// both managed backends without replacing the code already shipped by default.
func TestInitExampleFillsOnlyPlaceholderFolders(t *testing.T) {
	for _, framework := range []string{"adk", "langgraph"} {
		t.Run(framework, func(t *testing.T) {
			target := filepath.Join(t.TempDir(), "example-agent")
			if _, _, err := executeForTest(t, defaultSystem(), "init", target,
				"--framework", framework, "--example"); err != nil {
				t.Fatal(err)
			}
			assertManagedFolderExamples(t, target)
			assertOnlyPlaceholderResources(t, target)
			defaults := scaffoldFilesForMode("example-agent", framework, "managed", false)
			for _, path := range []string{"agent.py", "config.yaml", "agent-card.yaml",
				"instructions.md", "pyproject.toml", "harnest.lock", "lifecycle/storage.py"} {
				if actual := string(mustReadTestFile(t, filepath.Join(target, path))); actual != defaults[path] {
					t.Fatalf("--example replaced existing default content in %s", path)
				}
			}
			if _, err := os.Stat(filepath.Join(target, "lifecycle", "_example.py")); !os.IsNotExist(err) {
				t.Fatalf("lifecycle already has default code and needs no extra sample: %v", err)
			}
		})
	}
}

// assertManagedFolderExamples requires usable source in every guide-only folder
// and preserves native formats where the compiler does not consume Python.
func assertManagedFolderExamples(t *testing.T, target string) {
	t.Helper()
	assertFilesContain(t, target, map[string]string{
		"lib/_example.py":                                         "def normalize(",
		"models/_example.py":                                      "class Message(BaseModel)",
		"tools/_example.py":                                       "from harnest.tool import tool",
		"tasks/_example.py":                                       "@task(queue=",
		"cron/_example.py":                                        "daily_report = Cron(",
		"subagents/_example.py":                                   "helper = Agent(",
		"mcp/_example.py":                                         "def client():",
		"extensions/_example/extension.py":                        "class StarterExtension(Extension)",
		"extensions/_example/extension.yaml":                      "name: starter_runtime",
		"plugins/_example_agent/plugin.json":                      "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
		"plugins/_example_agent/skills/starter-guidance/SKILL.md": "name: starter-guidance",
		"skills/_example/SKILL.md":                                "name: getting-started",
		"sandbox/_example.py":                                     "network=False",
		"evals/_example.evalset.json":                             "answers_greeting",
		"tests/unit/_example.py":                                  "tools[\"echo\"]",
		"tests/smoke/_example.py":                                 "def test_health(client)",
	})
}

func TestInitRefusesNonEmptyDirectory(t *testing.T) {
	target := filepath.Join(t.TempDir(), "existing-agent")
	if err := os.Mkdir(target, 0o755); err != nil {
		t.Fatal(err)
	}
	existing := filepath.Join(target, "keep.txt")
	if err := os.WriteFile(existing, []byte("keep\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, _, err := executeForTest(t, defaultSystem(), "init", target)
	if err == nil || !strings.Contains(err.Error(), "not empty") {
		t.Fatalf("got error %v, want non-empty refusal", err)
	}
	contents, readErr := os.ReadFile(existing)
	if readErr != nil || string(contents) != "keep\n" {
		t.Fatalf("existing content was changed: %q, %v", contents, readErr)
	}
}

type failingScaffoldWriter struct {
	file *os.File
}

func (w failingScaffoldWriter) Write(contents []byte) (int, error) {
	written, _ := w.file.Write(contents[:min(4, len(contents))])
	return written, errors.New("simulated write failure")
}

func (w failingScaffoldWriter) Close() error { return w.file.Close() }

func TestCreateScaffoldFileRemovesPartialWrite(t *testing.T) {
	path := filepath.Join(t.TempDir(), "partial.py")
	opener := func(path string) (io.WriteCloser, error) {
		file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
		return failingScaffoldWriter{file: file}, err
	}

	err := createScaffoldFileWith(path, "complete contents", opener)

	if err == nil || !strings.Contains(err.Error(), "simulated write failure") {
		t.Fatalf("got error %v, want simulated write failure", err)
	}
	if _, statErr := os.Stat(path); !os.IsNotExist(statErr) {
		t.Fatalf("partial scaffold file remains after failure: %v", statErr)
	}
}

func TestInitSupportsLangGraphAndAdvancedMode(t *testing.T) {
	managed := filepath.Join(t.TempDir(), "langgraph-agent")
	if _, _, err := executeForTest(
		t, defaultSystem(), "init", managed, "--framework", "langgraph",
	); err != nil {
		t.Fatal(err)
	}
	assertManagedLangGraphScaffold(t, managed)

	advanced := filepath.Join(t.TempDir(), "advanced-agent")
	if _, _, err := executeForTest(
		t, defaultSystem(), "init", advanced,
		"--framework", "langgraph", "--mode", "advanced",
	); err != nil {
		t.Fatal(err)
	}
	assertAdvancedLangGraphScaffold(t, advanced)
}

// TestInitAdvancedADKUsesCanonicalOpenAIEnvironment keeps native ADK scaffolds
// on the same model credential contract as managed and LangGraph projects.
func TestInitAdvancedADKUsesCanonicalOpenAIEnvironment(t *testing.T) {
	target := filepath.Join(t.TempDir(), "advanced-adk-agent")
	if _, _, err := executeForTest(
		t, defaultSystem(), "init", target, "--framework", "adk", "--mode", "advanced",
	); err != nil {
		t.Fatal(err)
	}
	source := string(mustReadTestFile(t, filepath.Join(target, "agent.py")))
	assertContainsAll(t, "advanced ADK scaffold", source, []string{
		"from harnest.model import LiteLLMModel",
		"model=LiteLLMModel.from_openai_environment().build(),",
	})
}

// assertScaffoldModelEnvironment keeps model selection non-secret and ensures
// generated projects do not advertise the retired provider-specific defaults.
func assertScaffoldModelEnvironment(t *testing.T, bundle engine.Bundle) {
	t.Helper()
	if bundle.Config.Spec.Environment["OPENAI_MODEL"] != "gpt-4.1-mini" ||
		bundle.Config.Spec.Environment["OPENAI_BASE_URL"] != "https://api.openai.com/v1" {
		t.Fatalf("generated environment is missing OpenAI defaults: %v", bundle.Config.Spec.Environment)
	}
	for _, forbidden := range []string{"LITELLM_MODEL", "LITELLM_API_BASE", "OPENAI_API_KEY"} {
		if _, exists := bundle.Config.Spec.Environment[forbidden]; exists {
			t.Fatalf("generated environment contains unsupported or secret setting %q", forbidden)
		}
	}
}

func assertContainsAll(t *testing.T, label, contents string, expected []string) {
	t.Helper()
	for _, value := range expected {
		if !strings.Contains(contents, value) {
			t.Fatalf("%s is missing %q:\n%s", label, value, contents)
		}
	}
}

func assertDirectories(t *testing.T, root string, paths []string) {
	t.Helper()
	for _, relative := range paths {
		info, err := os.Stat(filepath.Join(root, filepath.FromSlash(relative)))
		if err != nil || !info.IsDir() {
			t.Fatalf("generated directory %s is missing: %v", relative, err)
		}
	}
}

func assertFilesContain(t *testing.T, root string, expected map[string]string) {
	t.Helper()
	for relative, needle := range expected {
		contents, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(relative)))
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(string(contents), needle) {
			t.Fatalf("generated %s is missing %q:\n%s", relative, needle, contents)
		}
	}
}

func assertManagedLangGraphScaffold(t *testing.T, directory string) {
	t.Helper()
	bundle, err := engine.LoadBundle(directory)
	if err != nil {
		t.Fatal(err)
	}
	if bundle.Config.Spec.Framework.Name != "langgraph" || bundle.Config.Spec.Framework.EffectiveMode() != "managed" {
		t.Fatalf("unexpected managed framework: %#v", bundle.Config.Spec.Framework)
	}
	source, err := os.ReadFile(filepath.Join(directory, "agent.py"))
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "managed scaffold", string(source), []string{
		"from harnest.agent import Agent",
		"root_agent = Agent(",
		"model=LiteLLMModel.from_openai_environment(),",
	})
	if _, err := os.Stat(filepath.Join(directory, "subagents", "helper.py")); !os.IsNotExist(err) {
		t.Fatalf("managed LangGraph scaffold must not create an implicit subagent: %v", err)
	}
	assertFilesExist(t, directory, []string{"subagents/_README.md", "evals/_README.md"})
	assertOnlyPlaceholderResources(t, directory)
}

func assertFilesExist(t *testing.T, root string, paths []string) {
	t.Helper()
	for _, relative := range paths {
		if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(relative))); err != nil {
			t.Fatalf("generated file %s is missing: %v", relative, err)
		}
	}
}

func assertAdvancedLangGraphScaffold(t *testing.T, directory string) {
	t.Helper()
	bundle, err := engine.LoadBundle(directory)
	if err != nil {
		t.Fatal(err)
	}
	if bundle.Config.Spec.Framework.EffectiveMode() != "advanced" {
		t.Fatalf("unexpected advanced framework: %#v", bundle.Config.Spec.Framework)
	}
	source, err := os.ReadFile(filepath.Join(directory, "agent.py"))
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "advanced scaffold", string(source), []string{
		"from harnest.agent import Agent",
		"root_agent = Agent.advanced(",
		"from langchain.agents import create_agent",
		"model=LiteLLMModel.from_openai_environment().build_langgraph(),",
	})
	if strings.Contains(string(source), "NativeApp") {
		t.Fatalf("advanced scaffold still exposes NativeApp:\n%s", source)
	}
	assertFilesExist(t, directory, []string{"tools/_README.md"})
	assertFilesContain(t, directory, map[string]string{
		"lib/_README.md":    "from harnest.lib.audit import record_change",
		"models/_README.md": "from harnest.models.support import",
		"tools/_README.md":  "Advanced mode owns framework wiring",
		"tasks/_README.md":  "Harnest discovers tasks in both authoring modes",
		"cron/_README.md":   "Harnest owns scheduling in both authoring modes",
	})
	assertOnlyPlaceholderResources(t, directory)
}

func assertOnlyPlaceholderResources(t *testing.T, root string) {
	t.Helper()
	for _, directory := range append([]string{"lib", "models"}, managedResourceDirectories...) {
		entries, err := os.ReadDir(filepath.Join(root, directory))
		if err != nil {
			t.Fatalf("read advanced optional folder %s: %v", directory, err)
		}
		for _, entry := range entries {
			if isRequiredStoreResource(directory, entry.Name()) {
				continue
			}
			if !strings.HasPrefix(entry.Name(), ".") && !strings.HasPrefix(entry.Name(), "_") {
				t.Fatalf("advanced optional folder %s contains discoverable resource %s", directory, entry.Name())
			}
		}
	}
}

func isRequiredStoreResource(directory, name string) bool {
	if directory == "lib" {
		return name == "storage.py"
	}
	return directory == "lifecycle" && name == "storage.py"
}

func TestPythonResolutionPrecedence(t *testing.T) {
	available := map[string]string{
		"flag-python":                            "/resolved/flag-python",
		"env-python":                             "/resolved/env-python",
		"/runtime/bin/python":                    "/resolved/managed-python",
		"/home/test/.harnest/runtime/bin/python": "/resolved/default-managed-python",
		"python3":                                "/resolved/python3",
	}
	environment := map[string]string{
		"HARNEST_PYTHON":      "env-python",
		"HARNEST_RUNTIME_DIR": "/runtime",
	}
	sys := system{
		getenv:      func(key string) string { return environment[key] },
		userHomeDir: func() (string, error) { return "/home/test", nil },
		lookPath: func(value string) (string, error) {
			if resolved, exists := available[value]; exists {
				return resolved, nil
			}
			return "", os.ErrNotExist
		},
		commandContext: defaultSystem().commandContext,
	}
	app := application{system: sys, pythonFlag: "flag-python"}
	selection, err := app.resolvePython()
	if err != nil || selection.Executable != "/resolved/flag-python" {
		t.Fatalf("flag resolution = %#v, %v", selection, err)
	}
	app.pythonFlag = ""
	selection, err = app.resolvePython()
	if err != nil || selection.Executable != "/resolved/env-python" {
		t.Fatalf("environment resolution = %#v, %v", selection, err)
	}
	delete(environment, "HARNEST_PYTHON")
	selection, err = app.resolvePython()
	if err != nil || selection.Executable != "/resolved/managed-python" {
		t.Fatalf("managed resolution = %#v, %v", selection, err)
	}
	delete(available, "/runtime/bin/python")
	selection, err = app.resolvePython()
	if err != nil || selection.Executable != "/resolved/python3" {
		t.Fatalf("PATH resolution = %#v, %v", selection, err)
	}
}

func TestCompileDelegatesToSelectedPython(t *testing.T) {
	target := filepath.Join(t.TempDir(), "compile-agent")
	if err := createScaffold(target, "compile-agent"); err != nil {
		t.Fatal(err)
	}
	record := filepath.Join(t.TempDir(), "arguments.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	python := writeExecutable(t, `#!/bin/sh
printf '%s\n' "$@" > "$HARNEST_TEST_RECORD"
`)
	output := filepath.Join(t.TempDir(), "artifact")
	_, _, err := executeForTest(t, defaultSystem(), "--python", python, "compile", target, "--output", output)
	if err != nil {
		t.Fatal(err)
	}
	arguments, err := os.ReadFile(record)
	if err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{"-m\nharnest.cli\ncompile\n", target, "--output\n" + output, "--entrypoint\nagent:root_agent", "--framework\nadk", "--mode\nmanaged"} {
		if !strings.Contains(string(arguments), expected) {
			t.Fatalf("delegated arguments are missing %q:\n%s", expected, arguments)
		}
	}
}

func TestUpgradeDelegatesReadOnlyPlanAndExplicitApply(t *testing.T) {
	target := filepath.Join(t.TempDir(), "upgrade-agent")
	record := filepath.Join(t.TempDir(), "arguments.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	python := writeExecutable(t, `#!/bin/sh
printf '%s\n' "$@" > "$HARNEST_TEST_RECORD"
`)
	if _, _, err := executeForTest(
		t, defaultSystem(), "--python", python, "upgrade", target,
	); err != nil {
		t.Fatal(err)
	}
	arguments := string(mustReadTestFile(t, record))
	assertContainsAll(t, "upgrade plan arguments", arguments, []string{
		"-m\nharnest.cli\nupgrade\n", target,
	})
	if strings.Contains(arguments, "--apply") {
		t.Fatalf("read-only upgrade unexpectedly delegated --apply:\n%s", arguments)
	}
	if _, _, err := executeForTest(
		t, defaultSystem(), "--python", python, "upgrade", target, "--apply",
	); err != nil {
		t.Fatal(err)
	}
	arguments = string(mustReadTestFile(t, record))
	if !strings.Contains(arguments, "--apply") {
		t.Fatalf("destructive upgrade did not delegate --apply:\n%s", arguments)
	}
}

func TestEvalTrajectoryIsValidatedAndDelegated(t *testing.T) {
	target := filepath.Join(t.TempDir(), "eval-agent")
	resultOutput := filepath.Join(t.TempDir(), "eval-result.json")
	if err := createScaffold(target, "eval-agent"); err != nil {
		t.Fatal(err)
	}
	record := filepath.Join(t.TempDir(), "arguments.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	python := writeExecutable(t, `#!/bin/sh
printf '%s\n' "$@" > "$HARNEST_TEST_RECORD"
`)
	_, _, err := executeForTest(
		t, defaultSystem(), "--python", python, "test", target,
		"--evals", "--eval-trajectory", "strict", "--no-output",
		"--eval-output", resultOutput,
	)
	if err != nil {
		t.Fatal(err)
	}
	arguments := mustReadTestFile(t, record)
	assertContainsAll(t, "delegated eval arguments", string(arguments), []string{
		"--evals\n--eval-trajectory\nstrict\n--no-output\n--eval-output\n" + resultOutput,
	})
	_, _, err = executeForTest(
		t, defaultSystem(), "--python", python, "test", target,
		"--no-output",
	)
	if err != nil {
		t.Fatal(err)
	}
	arguments = mustReadTestFile(t, record)
	if !strings.Contains(string(arguments), "--no-output") {
		t.Fatalf("delegated test arguments are missing --no-output:\n%s", arguments)
	}
	_, _, err = executeForTest(
		t, defaultSystem(), "--python", python, "test", target,
		"--eval-output", resultOutput,
	)
	if err == nil || !strings.Contains(err.Error(), "--eval-output requires --evals") {
		t.Fatalf("got error %v, want eval output validation", err)
	}
	_, _, err = executeForTest(
		t, defaultSystem(), "--python", python, "test", target,
		"--eval-trajectory", "approximate",
	)
	if err == nil || !strings.Contains(err.Error(), "business or strict") {
		t.Fatalf("got error %v, want trajectory validation", err)
	}
	testHelp, _, err := executeForTest(t, defaultSystem(), "test", "--help")
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "test help", testHelp, []string{
		"--eval-output string",
		"write the complete structured eval result to this JSON file",
	})
}

func TestEvalCommandReceivesCanonicalModelEnvironment(t *testing.T) {
	target := filepath.Join(t.TempDir(), "eval-environment-agent")
	if err := createScaffold(target, "eval-environment-agent"); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(target, "config.yaml")
	configured := strings.Replace(
		string(mustReadTestFile(t, configPath)),
		"OPENAI_MODEL: gpt-4.1-mini",
		"OPENAI_MODEL: openai/gpt-configured-for-eval",
		1,
	)
	if err := os.WriteFile(configPath, []byte(configured), 0o644); err != nil {
		t.Fatal(err)
	}

	record := filepath.Join(t.TempDir(), "environment.txt")
	providerSecret := "openai-secret-must-not-be-recorded"
	t.Setenv("HARNEST_TEST_RECORD", record)
	t.Setenv("OPENAI_API_KEY", providerSecret)
	t.Setenv("OPENAI_MODEL", "openai/gpt-parent-must-be-overridden")
	// Record presence markers only: captured test output must never disclose
	// credentials while verifying Harnest's canonical model environment.
	python := writeExecutable(t, `#!/bin/sh
api_key=missing
if [ -n "$OPENAI_API_KEY" ]; then api_key=present; fi
printf 'api_key=%s\nmodel=%s\n' "$api_key" "$OPENAI_MODEL" > "$HARNEST_TEST_RECORD"
`)
	stdout, stderr, err := executeForTest(
		t, defaultSystem(), "--python", python, "test", target, "--evals",
	)
	if err != nil {
		t.Fatal(err)
	}
	markers := string(mustReadTestFile(t, record))
	assertContainsAll(t, "canonical eval environment", markers, []string{
		"api_key=present",
		"model=openai/gpt-configured-for-eval",
	})
	if strings.Contains(markers, "openai/gpt-parent-must-be-overridden") {
		t.Fatalf("configured OPENAI_MODEL did not override the parent environment")
	}
	for label, value := range map[string]string{
		"record": markers,
		"stdout": stdout,
		"stderr": stderr,
	} {
		if strings.Contains(value, providerSecret) {
			t.Fatalf("%s disclosed the provider credential", label)
		}
	}
}

func TestServeRunsGeneratedLauncherWithSelectedPython(t *testing.T) {
	target, record, python := serveRecordingFixture(t)
	artifact := filepath.Join(t.TempDir(), "artifact")
	_, _, err := executeForTest(
		t, defaultSystem(), "--python", python, "serve", target,
		"--output", artifact, "--host", "0.0.0.0", "--port", "9090",
		"--request-timeout", "30", "--max-concurrency", "4", "--allow-remote",
	)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(mustReadTestFile(t, record))), "\n")
	if len(lines) != 2 {
		t.Fatalf("got calls %q, want compile and serve", lines)
	}
	want := "CALL\t" + filepath.Join(artifact, "harnest-agent") +
		"\tserve\t--host\t0.0.0.0\t--port\t9090\t--request-timeout\t30" +
		"\t--max-concurrency\t4\t--allow-remote"
	if lines[1] != want {
		t.Fatalf("selected Python got %q, want only explicit override %q", lines[1], want)
	}
}

func TestServeUsesCompiledServerDefaultsWithoutOverrides(t *testing.T) {
	target, record, python := serveRecordingFixture(t)
	artifact := filepath.Join(t.TempDir(), "artifact")
	_, _, err := executeForTest(
		t, defaultSystem(), "--python", python, "serve", target,
		"--output", artifact,
	)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(mustReadTestFile(t, record))), "\n")
	want := "CALL\t" + filepath.Join(artifact, "harnest-agent") + "\tserve"
	if len(lines) != 2 || lines[1] != want {
		t.Fatalf("serve overrode compiled server.yaml defaults: %q", lines)
	}
}

func serveRecordingFixture(t *testing.T) (string, string, string) {
	t.Helper()
	target := filepath.Join(t.TempDir(), "serve-agent")
	if err := createScaffold(target, "serve-agent"); err != nil {
		t.Fatal(err)
	}
	record := filepath.Join(t.TempDir(), "calls.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	python := writeExecutable(t, `#!/bin/sh
printf 'CALL' >> "$HARNEST_TEST_RECORD"
for value in "$@"; do printf '\t%s' "$value" >> "$HARNEST_TEST_RECORD"; done
printf '\n' >> "$HARNEST_TEST_RECORD"
if [ "$1" = "-m" ]; then
  output=""
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--output" ]; then shift; output="$1"; fi
    shift
  done
  mkdir -p "$output"
  printf 'generated launcher\n' > "$output/harnest-agent"
fi
`)
	return target, record, python
}

func TestDoctorReportsReadyRuntime(t *testing.T) {
	python := writeExecutable(t, `#!/bin/sh
printf '%s\n' '{"executable":"/runtime/python","python":"3.12.8","supported":true,"packages":[{"name":"harnest","ok":true,"version":"0.1.0","error":""},{"name":"google-adk","ok":true,"version":"2.0.0","error":""},{"name":"litellm","ok":true,"version":"1.84.0","error":""},{"name":"pytest","ok":true,"version":"8.4.0","error":""}]}'
`)
	stdout, _, err := executeForTest(t, defaultSystem(), "--python", python, "doctor")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout, "Harnest is ready") || !strings.Contains(stdout, "google-adk 2.0.0") {
		t.Fatalf("unexpected doctor output:\n%s", stdout)
	}
}

func executeForTest(t *testing.T, sys system, arguments ...string) (string, string, error) {
	t.Helper()
	command := newRootCommand(sys, "test-version")
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	command.SetOut(&stdout)
	command.SetErr(&stderr)
	command.SetIn(strings.NewReader(""))
	command.SetArgs(arguments)
	err := command.ExecuteContext(context.Background())
	return stdout.String(), stderr.String(), err
}

func writeExecutable(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "python")
	if err := os.WriteFile(path, []byte(contents), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}
