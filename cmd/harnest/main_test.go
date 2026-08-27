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
	for _, expected := range []string{"harnest skills install", "harnest init", "harnest mode advanced", "harnest test", "harnest compile", "harnest serve", "lib/", "tools/", "evals/"} {
		if !strings.Contains(stdout, expected) {
			t.Fatalf("help is missing %q:\n%s", expected, stdout)
		}
	}
	if strings.Contains(stdout, "plan") {
		t.Fatalf("standalone help unexpectedly mentions orchestrator plans:\n%s", stdout)
	}
}

func TestInitCreatesLoadableKebabNamedLiteLLMAgent(t *testing.T) {
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
	if bundle.Config.Spec.Environment["LITELLM_MODEL"] != "ollama_chat/qwen3.5:cloud" {
		t.Fatalf("generated environment is missing LiteLLM defaults: %v", bundle.Config.Spec.Environment)
	}
	agentSource, err := os.ReadFile(filepath.Join(target, "agent.py"))
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "generated agent.py", string(agentSource), []string{
		`name="support_agent"`,
		"from harnest.agent import Agent",
		"from harnest.graph import START, Edge, Graph",
		`"respond": Agent(`,
	})
	skillManifest, err := os.ReadFile(filepath.Join(target, "skills", "getting-started", "SKILL.md"))
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "starter skill", string(skillManifest), []string{
		"---\nname: getting-started\n",
		"description: Apply the agent's core instructions",
		"# Getting started",
	})
	assertDirectories(t, target, []string{
		"lib", "tools", "subagents", "mcp", "extensions", "plugins", "sandbox", "skills", "evals",
		"tests/unit", "tests/smoke",
	})
	assertFilesContain(t, target, map[string]string{
		"lib/_README.md":                                   "from harnest.lib.audit import record_change",
		"tools/echo.py":                                    "from harnest.tool import tool",
		"subagents/__init__.py":                            "Add direct graph agents",
		"mcp/_README.md":                                   "Add direct MCP client connections",
		"extensions/starter/lifecycle.py":                  "extension = Extension(name=\"starter\")",
		"plugins/starter/mcp/starter.py":                   "from harnest.mcp import MCPClient",
		"plugins/starter/skills/starter-guidance/SKILL.md": "name: starter-guidance",
		"sandbox/_README.md":                               "Sandbox.container",
		"sandbox/_example.py":                              "from harnest.sandbox import Sandbox",
	})
	if _, err := os.Stat(filepath.Join(target, "plugins", "starter", "agents")); !os.IsNotExist(err) {
		t.Fatalf("generated plugin must not contain agents: %v", err)
	}
	assertIgnoredLibraryGuide(t, target)
}

func assertIgnoredLibraryGuide(t *testing.T, root string) {
	t.Helper()
	entries, err := os.ReadDir(filepath.Join(root, "lib"))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != "_README.md" {
		t.Fatalf("generated lib must contain only ignored guidance: %v", entries)
	}
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
	assertContainsAll(t, "managed scaffold", string(source), []string{"from harnest.agent import Agent", "root_agent = Graph(", `"respond": Agent(`})
	if _, err := os.Stat(filepath.Join(directory, "subagents", "helper.py")); !os.IsNotExist(err) {
		t.Fatalf("managed LangGraph scaffold must not create an implicit subagent: %v", err)
	}
	assertFilesExist(t, directory, []string{"subagents/_README.md", "evals/_README.md"})
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
	assertContainsAll(t, "advanced scaffold", string(source), []string{"from harnest.agent import Agent", "root_agent = Agent.advanced(", "from langchain.agents import create_agent"})
	if strings.Contains(string(source), "NativeApp") {
		t.Fatalf("advanced scaffold still exposes NativeApp:\n%s", source)
	}
	assertFilesExist(t, directory, []string{"tools/_README.md"})
	assertFilesContain(t, directory, map[string]string{
		"lib/_README.md": "from harnest.lib.audit import record_change",
	})
	assertOnlyPlaceholderResources(t, directory)
}

func assertOnlyPlaceholderResources(t *testing.T, root string) {
	t.Helper()
	for _, directory := range append([]string{"lib"}, managedResourceDirectories...) {
		entries, err := os.ReadDir(filepath.Join(root, directory))
		if err != nil {
			t.Fatalf("read advanced optional folder %s: %v", directory, err)
		}
		for _, entry := range entries {
			if !strings.HasPrefix(entry.Name(), ".") && !strings.HasPrefix(entry.Name(), "_") {
				t.Fatalf("advanced optional folder %s contains discoverable resource %s", directory, entry.Name())
			}
		}
	}
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

func TestServeRunsGeneratedLauncherWithSelectedPython(t *testing.T) {
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
	artifact := filepath.Join(t.TempDir(), "artifact")
	_, _, err := executeForTest(
		t, defaultSystem(), "--python", python, "serve", target,
		"--output", artifact, "--port", "9090",
	)
	if err != nil {
		t.Fatal(err)
	}
	calls, err := os.ReadFile(record)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(calls)), "\n")
	if len(lines) != 2 {
		t.Fatalf("got calls %q, want compile and serve", calls)
	}
	if !strings.Contains(lines[1], filepath.Join(artifact, "harnest-agent")) || !strings.Contains(lines[1], "--port\t9090") {
		t.Fatalf("selected Python did not run the generated launcher: %s", lines[1])
	}
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
