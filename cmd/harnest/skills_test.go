package main

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSkillsShowPrintsEmbeddedAuthoringSkill(t *testing.T) {
	stdout, _, err := executeForTest(t, defaultSystem(), "skills", "show")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout, "name: harnest-authoring") {
		t.Fatalf("unexpected authoring skill output:\n%s", stdout)
	}
}

func TestSkillsInstallUsesExplicitCodingAgentTarget(t *testing.T) {
	project := t.TempDir()
	stdout, _, err := executeForTest(
		t,
		defaultSystem(),
		"skills", "install", "--project", project, "--target", "claude",
	)
	if err != nil {
		t.Fatal(err)
	}
	destination := filepath.Join(project, ".claude", "skills", authoringSkillName)
	assertEmbeddedSkillInstalled(t, destination)
	if !strings.Contains(stdout, destination) || !strings.Contains(stdout, "for claude") {
		t.Fatalf("install output does not identify target and destination:\n%s", stdout)
	}
	if _, err := os.Stat(filepath.Join(project, "skills", authoringSkillName)); !os.IsNotExist(err) {
		t.Fatalf("authoring skill was installed into runtime skills/: %v", err)
	}
}

func TestSkillsInstallDetectsInvokingCodingAgent(t *testing.T) {
	project := t.TempDir()
	sys := defaultSystem()
	sys.getenv = func(key string) string {
		if key == "CODEX_SHELL" {
			return "1"
		}
		return ""
	}
	_, _, err := executeForTest(t, sys, "skills", "install", "--project", project)
	if err != nil {
		t.Fatal(err)
	}
	assertEmbeddedSkillInstalled(
		t,
		filepath.Join(project, ".agents", "skills", authoringSkillName),
	)
}

func TestSkillsInstallPrefersActiveAgentOverInstalledCodex(t *testing.T) {
	project := t.TempDir()
	sys := defaultSystem()
	sys.getenv = func(key string) string {
		switch key {
		case "CLAUDE_CODE":
			return "1"
		case "CODEX_SHELL", "CODEX_HOME":
			return "/home/test/.codex"
		default:
			return ""
		}
	}
	_, _, err := executeForTest(t, sys, "skills", "install", "--project", project)
	if err != nil {
		t.Fatal(err)
	}
	assertEmbeddedSkillInstalled(
		t,
		filepath.Join(project, ".claude", "skills", authoringSkillName),
	)
}

func TestSkillsInstallDetectsClaudeAndCursorEnvironments(t *testing.T) {
	for _, testCase := range []struct {
		name        string
		environment string
		directory   string
	}{
		{name: "claude", environment: "CLAUDECODE", directory: ".claude"},
		{name: "cursor", environment: "CURSOR_AGENT", directory: ".cursor"},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			project := t.TempDir()
			sys := defaultSystem()
			sys.getenv = func(key string) string {
				if key == testCase.environment {
					return "1"
				}
				return ""
			}
			_, _, err := executeForTest(t, sys, "skills", "install", "--project", project)
			if err != nil {
				t.Fatal(err)
			}
			assertEmbeddedSkillInstalled(
				t,
				filepath.Join(project, testCase.directory, "skills", authoringSkillName),
			)
		})
	}
}

func TestSkillsInstallFallsBackToAgentSkillsConvention(t *testing.T) {
	project := t.TempDir()
	sys := defaultSystem()
	sys.getenv = func(string) string { return "" }
	_, _, err := executeForTest(t, sys, "skills", "install", "--project", project)
	if err != nil {
		t.Fatal(err)
	}
	assertEmbeddedSkillInstalled(
		t,
		filepath.Join(project, ".agents", "skills", authoringSkillName),
	)
}

func TestSkillsInstallRefusesOverwriteWithoutForce(t *testing.T) {
	project := t.TempDir()
	arguments := []string{"skills", "install", "--project", project, "--target", "cursor"}
	if _, _, err := executeForTest(t, defaultSystem(), arguments...); err != nil {
		t.Fatal(err)
	}
	destination := filepath.Join(project, ".cursor", "skills", authoringSkillName)
	marker := filepath.Join(destination, "old.txt")
	if err := os.WriteFile(marker, []byte("old"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, _, err := executeForTest(t, defaultSystem(), arguments...); err == nil ||
		!strings.Contains(err.Error(), "already exists") {
		t.Fatalf("got error %v, want existing-install refusal", err)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("refused install changed existing contents: %v", err)
	}
	arguments = append(arguments, "--force")
	if _, _, err := executeForTest(t, defaultSystem(), arguments...); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatalf("forced install retained stale content: %v", err)
	}
	assertEmbeddedSkillInstalled(t, destination)
}

func TestSkillsInstallRejectsUnknownTarget(t *testing.T) {
	_, _, err := executeForTest(
		t,
		defaultSystem(),
		"skills", "install", "--project", t.TempDir(), "--target", "unknown",
	)
	if err == nil || !strings.Contains(err.Error(), "unknown coding agent target") {
		t.Fatalf("got error %v, want target validation", err)
	}
}

func assertEmbeddedSkillInstalled(t *testing.T, destination string) {
	t.Helper()
	err := fs.WalkDir(authoringSkill, authoringSkillRoot, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		relative := strings.TrimPrefix(path, authoringSkillRoot+"/")
		want, err := authoringSkill.ReadFile(path)
		if err != nil {
			return err
		}
		got, err := os.ReadFile(filepath.Join(destination, filepath.FromSlash(relative)))
		if err != nil {
			return err
		}
		if string(got) != string(want) {
			t.Fatalf("installed %s does not match the embedded file", relative)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("inspect installed authoring skill: %v", err)
	}
}
