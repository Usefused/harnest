package main

import (
	"fmt"
	"path/filepath"
	"testing"
)

// TestAddChannelScaffoldsProviderNeutralBinding keeps platform policy free of transport code.
func TestAddChannelScaffoldsProviderNeutralBinding(t *testing.T) {
	root := filepath.Join(t.TempDir(), "channel-agent")
	if _, _, err := executeForTest(t, defaultSystem(), "init", root, "--minimal"); err != nil {
		t.Fatal(err)
	}
	stdout, _, err := executeForTest(
		t, defaultSystem(), "add", "channel", "slack", "--via", "fused", "--project", root,
	)
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "default channel output", stdout, []string{
		`Added channel "slack"`, "harnest channels inspect .",
	})
	slack := string(mustReadTestFile(t, filepath.Join(root, "channels", "slack.py")))
	assertContainsAll(t, "default channel source", slack, []string{
		"from harnest.channels import ChannelBinding",
		"def binding() -> ChannelBinding:",
		`platform="slack"`,
		`extension="fused"`,
	})

	invalid := [][]string{
		{},
		{"--via", "Fused"},
		{"--via", ""},
	}
	for index, arguments := range invalid {
		command := []string{"add", "channel", fmt.Sprintf("invalid-%d", index), "--project", root}
		command = append(command, arguments...)
		if _, _, err := executeForTest(t, defaultSystem(), command...); err == nil {
			t.Fatalf("invalid channel arguments were accepted: %v", arguments)
		}
	}
}

// TestChannelsDelegatesConfiguredAgentPolicy keeps discovery and fixtures on the agent interpreter.
func TestChannelsDelegatesConfiguredAgentPolicy(t *testing.T) {
	target := filepath.Join(t.TempDir(), "channels-agent")
	if err := createScaffold(target, "channels-agent"); err != nil {
		t.Fatal(err)
	}
	record := filepath.Join(t.TempDir(), "arguments.txt")
	t.Setenv("HARNEST_TEST_RECORD", record)
	python := writeExecutable(t, `#!/bin/sh
printf '%s\n' "$@" > "$HARNEST_TEST_RECORD"
`)
	resolved, err := filepath.EvalSymlinks(target)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := executeForTest(t, defaultSystem(), "--python", python, "channels", "inspect", target, "--platform", "slack", "--json"); err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "channels inspect delegated arguments", string(mustReadTestFile(t, record)), []string{
		"harnest.cli", "channels\ninspect\nslack", "--project\n" + resolved, "--json",
	})

	if _, _, err := executeForTest(t, defaultSystem(), "--python", python, "channels", "test", target, "--fixture", "fixtures/slack.json"); err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "channels test delegated arguments", string(mustReadTestFile(t, record)), []string{
		"channels\ntest", "--fixture\nfixtures/slack.json",
	})
}

// TestChannelsHelpListsBothOperations pins the documented discovery workflow.
func TestChannelsHelpListsBothOperations(t *testing.T) {
	stdout, _, err := executeForTest(t, defaultSystem(), "channels", "--help")
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "channels help", stdout, []string{"inspect", "test"})
}
