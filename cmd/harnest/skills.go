package main

import (
	"embed"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"
)

const (
	authoringSkillRoot = "authoring_skill/harnest-authoring"
	authoringSkillName = "harnest-authoring"
)

// authoringSkill contains documentation for coding agents that modify Harnest
// projects. It is intentionally installed outside an agent's runtime skills/.
//
//go:embed authoring_skill/harnest-authoring
var authoringSkill embed.FS

type codingAgentTarget struct {
	name              string
	relativeSkillsDir string
}

var codingAgentTargets = map[string]codingAgentTarget{
	"agents":  {name: "agents", relativeSkillsDir: ".agents/skills"},
	"claude":  {name: "claude", relativeSkillsDir: ".claude/skills"},
	"codex":   {name: "codex", relativeSkillsDir: ".agents/skills"},
	"copilot": {name: "copilot", relativeSkillsDir: ".github/skills"},
	"cursor":  {name: "cursor", relativeSkillsDir: ".cursor/skills"},
}

func (a *application) newSkillsCommand() *cobra.Command {
	command := &cobra.Command{
		Use:   "skills",
		Short: "Show or install Harnest guidance for coding agents",
		Long: `Show or install the bundled Harnest authoring skill.

This skill teaches a coding agent how to create and modify Harnest projects. It
is separate from the runtime skills/ directory compiled into an agent.`,
	}
	command.AddCommand(a.newSkillsShowCommand(), a.newSkillsInstallCommand())
	return command
}

func (a *application) newSkillsShowCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "show",
		Short: "Print the bundled Harnest authoring skill",
		Args:  cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			contents, err := authoringSkill.ReadFile(authoringSkillRoot + "/SKILL.md")
			if err != nil {
				return fmt.Errorf("read embedded authoring skill: %w", err)
			}
			_, err = command.OutOrStdout().Write(contents)
			return err
		},
	}
}

func (a *application) newSkillsInstallCommand() *cobra.Command {
	var targetName string
	var projectDirectory string
	var force bool
	command := &cobra.Command{
		Use:   "install",
		Short: "Install the authoring skill for a coding agent",
		Args:  cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			project, err := filepath.Abs(projectDirectory)
			if err != nil {
				return fmt.Errorf("resolve project directory: %w", err)
			}
			info, err := os.Stat(project)
			if err != nil {
				return fmt.Errorf("inspect project directory: %w", err)
			}
			if !info.IsDir() {
				return fmt.Errorf("project path %s is not a directory", project)
			}

			target, err := a.resolveCodingAgentTarget(targetName, project)
			if err != nil {
				return err
			}
			destination := filepath.Join(
				project,
				filepath.FromSlash(target.relativeSkillsDir),
				authoringSkillName,
			)
			if err := installAuthoringSkill(destination, force); err != nil {
				return err
			}
			fmt.Fprintf(
				command.OutOrStdout(),
				"Installed Harnest authoring skill for %s at %s\n",
				target.name,
				destination,
			)
			return nil
		},
	}
	command.Flags().StringVar(
		&targetName,
		"target",
		"auto",
		"coding agent target: auto, agents, claude, codex, copilot, or cursor",
	)
	command.Flags().StringVar(
		&projectDirectory,
		"project",
		".",
		"project root where the coding-agent skill should be installed",
	)
	command.Flags().BoolVar(
		&force,
		"force",
		false,
		"replace an existing installed authoring skill",
	)
	return command
}

func (a *application) resolveCodingAgentTarget(requested, project string) (codingAgentTarget, error) {
	requested = strings.ToLower(strings.TrimSpace(requested))
	if requested != "" && requested != "auto" {
		target, exists := codingAgentTargets[requested]
		if !exists {
			return codingAgentTarget{}, fmt.Errorf(
				"unknown coding agent target %q; choose agents, claude, codex, copilot, or cursor",
				requested,
			)
		}
		return target, nil
	}

	for _, detection := range []struct {
		environment []string
		target      string
	}{
		{environment: []string{"CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT"}, target: "claude"},
		{environment: []string{"CURSOR_AGENT", "CURSOR_SANDBOX"}, target: "cursor"},
		{environment: []string{"GITHUB_COPILOT_CHAT"}, target: "copilot"},
		{environment: []string{"CODEX_SHELL"}, target: "codex"},
	} {
		for _, key := range detection.environment {
			if strings.TrimSpace(a.system.getenv(key)) != "" {
				return codingAgentTargets[detection.target], nil
			}
		}
	}
	if strings.Contains(strings.ToLower(a.system.getenv("TERM_PROGRAM")), "cursor") {
		return codingAgentTargets["cursor"], nil
	}
	// CODEX_HOME can remain set outside a running Codex session, so treat it as
	// a weaker hint than active-agent markers above.
	if strings.TrimSpace(a.system.getenv("CODEX_HOME")) != "" {
		return codingAgentTargets["codex"], nil
	}

	for _, candidate := range []string{".agents", ".claude", ".cursor"} {
		info, err := os.Stat(filepath.Join(project, candidate))
		if err == nil && info.IsDir() {
			switch candidate {
			case ".claude":
				return codingAgentTargets["claude"], nil
			case ".cursor":
				return codingAgentTargets["cursor"], nil
			default:
				return codingAgentTargets["agents"], nil
			}
		}
	}

	// Agent Skills is an open project-local convention and the safest fallback
	// when no invoking coding agent identifies itself.
	return codingAgentTargets["agents"], nil
}

func installAuthoringSkill(destination string, force bool) error {
	if info, err := os.Lstat(destination); err == nil {
		if !force {
			return fmt.Errorf(
				"authoring skill already exists at %s; pass --force to replace it",
				destination,
			)
		}
		if !info.IsDir() {
			return fmt.Errorf("authoring skill destination %s is not a directory", destination)
		}
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("inspect authoring skill destination: %w", err)
	}

	parent := filepath.Dir(destination)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return fmt.Errorf("create coding-agent skills directory: %w", err)
	}
	stagingRoot, err := os.MkdirTemp(parent, ".harnest-authoring-install-")
	if err != nil {
		return fmt.Errorf("create authoring skill staging directory: %w", err)
	}
	defer os.RemoveAll(stagingRoot)
	staged := filepath.Join(stagingRoot, authoringSkillName)
	if err := copyEmbeddedAuthoringSkill(staged); err != nil {
		return err
	}

	backup := filepath.Join(stagingRoot, "previous")
	hadPrevious := false
	if _, err := os.Lstat(destination); err == nil {
		hadPrevious = true
		if err := os.Rename(destination, backup); err != nil {
			return fmt.Errorf("stage existing authoring skill for replacement: %w", err)
		}
	}
	if err := os.Rename(staged, destination); err != nil {
		if hadPrevious {
			_ = os.Rename(backup, destination)
		}
		return fmt.Errorf("install authoring skill: %w", err)
	}
	return nil
}

func copyEmbeddedAuthoringSkill(destination string) error {
	return fs.WalkDir(authoringSkill, authoringSkillRoot, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(authoringSkillRoot, path)
		if err != nil {
			return err
		}
		target := filepath.Join(destination, filepath.FromSlash(relative))
		if entry.IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return fmt.Errorf("create embedded skill directory %s: %w", target, err)
			}
			return nil
		}
		contents, err := authoringSkill.ReadFile(path)
		if err != nil {
			return fmt.Errorf("read embedded skill file %s: %w", path, err)
		}
		if err := os.WriteFile(target, contents, 0o644); err != nil {
			return fmt.Errorf("write embedded skill file %s: %w", target, err)
		}
		return nil
	})
}
