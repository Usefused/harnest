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
	authoringSkillRoot      = "authoring_skill/harnest-authoring"
	authoringSkillName      = "harnest-authoring"
	authenticationSkillRoot = "authoring_skill/harnest-authentication"
	authenticationSkillName = "harnest-authentication"
)

type bundledCodingAgentSkill struct {
	name string
	root string
}

var bundledCodingAgentSkills = []bundledCodingAgentSkill{
	{name: authoringSkillName, root: authoringSkillRoot},
	{name: authenticationSkillName, root: authenticationSkillRoot},
}

// authoringSkill contains focused guidance for coding agents that modify
// Harnest projects. It is installed outside an agent's runtime skills/.
//
//go:embed authoring_skill/harnest-authoring authoring_skill/harnest-authentication
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

// newSkillsCommand exposes embedded coding guidance outside runtime agent skills.
func (a *application) newSkillsCommand() *cobra.Command {
	command := &cobra.Command{
		Use:   "skills",
		Short: "Show or install Harnest guidance for coding agents",
		Long: `Show or install the bundled Harnest coding-agent skills.

These skills cover general authoring and authentication/credential boundaries.
They are separate from the runtime skills/ directory compiled into an agent.`,
	}
	command.AddCommand(a.newSkillsShowCommand(), a.newSkillsInstallCommand())
	return command
}

// newSkillsShowCommand prints one explicitly selected embedded skill entrypoint.
func (a *application) newSkillsShowCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "show [skill]",
		Short: "Print a bundled Harnest coding-agent skill",
		Args:  cobra.MaximumNArgs(1),
		RunE: func(command *cobra.Command, args []string) error {
			name := authoringSkillName
			if len(args) == 1 {
				name = args[0]
			}
			skill, err := bundledSkill(name)
			if err != nil {
				return err
			}
			contents, err := authoringSkill.ReadFile(skill.root + "/SKILL.md")
			if err != nil {
				return fmt.Errorf("read embedded %s skill: %w", skill.name, err)
			}
			_, err = command.OutOrStdout().Write(contents)
			return err
		},
	}
}

// newSkillsInstallCommand installs the complete cooperating skill set together.
func (a *application) newSkillsInstallCommand() *cobra.Command {
	var targetName string
	var projectDirectory string
	var force bool
	command := &cobra.Command{
		Use:   "install",
		Short: "Install Harnest skills for a coding agent",
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
			destinations := skillDestinations(project, target)
			for _, destination := range destinations {
				if err := validateSkillDestination(destination.path, force); err != nil {
					return err
				}
			}
			for _, destination := range destinations {
				if err := installBundledSkill(destination.skill, destination.path, force); err != nil {
					return err
				}
			}
			fmt.Fprintf(
				command.OutOrStdout(),
				"Installed Harnest coding-agent skills for %s at %s\n",
				target.name,
				filepath.Dir(destinations[0].path),
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
		"project root where the coding-agent skills should be installed",
	)
	command.Flags().BoolVar(
		&force,
		"force",
		false,
		"replace existing installed Harnest coding-agent skills",
	)
	return command
}

type skillDestination struct {
	skill bundledCodingAgentSkill
	path  string
}

// skillDestinations maps every bundled skill into one coding-agent convention.
func skillDestinations(project string, target codingAgentTarget) []skillDestination {
	directory := filepath.Join(project, filepath.FromSlash(target.relativeSkillsDir))
	destinations := make([]skillDestination, 0, len(bundledCodingAgentSkills))
	for _, skill := range bundledCodingAgentSkills {
		destinations = append(destinations, skillDestination{
			skill: skill,
			path:  filepath.Join(directory, skill.name),
		})
	}
	return destinations
}

// bundledSkill resolves only declared skill names from the embedded filesystem.
func bundledSkill(name string) (bundledCodingAgentSkill, error) {
	// Selection is explicit so `show` cannot read arbitrary embedded files.
	for _, skill := range bundledCodingAgentSkills {
		if name == skill.name {
			return skill, nil
		}
	}
	return bundledCodingAgentSkill{}, fmt.Errorf(
		"unknown Harnest skill %q; choose %s or %s",
		name,
		authoringSkillName,
		authenticationSkillName,
	)
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
	if target, found := a.detectActiveCodingAgent(); found {
		return target, nil
	}
	if target, found := detectProjectCodingAgent(project); found {
		return target, nil
	}
	// Agent Skills is an open project-local convention and the safest fallback
	// when no invoking coding agent identifies itself.
	return codingAgentTargets["agents"], nil
}

func (a *application) detectActiveCodingAgent() (codingAgentTarget, bool) {
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
				return codingAgentTargets[detection.target], true
			}
		}
	}
	if strings.Contains(strings.ToLower(a.system.getenv("TERM_PROGRAM")), "cursor") {
		return codingAgentTargets["cursor"], true
	}
	// CODEX_HOME can remain set outside a running Codex session, so treat it as
	// a weaker hint than active-agent markers above.
	if strings.TrimSpace(a.system.getenv("CODEX_HOME")) != "" {
		return codingAgentTargets["codex"], true
	}
	return codingAgentTarget{}, false
}

func detectProjectCodingAgent(project string) (codingAgentTarget, bool) {
	for _, candidate := range []string{".agents", ".claude", ".cursor"} {
		info, err := os.Stat(filepath.Join(project, candidate))
		if err == nil && info.IsDir() {
			switch candidate {
			case ".claude":
				return codingAgentTargets["claude"], true
			case ".cursor":
				return codingAgentTargets["cursor"], true
			default:
				return codingAgentTargets["agents"], true
			}
		}
	}
	return codingAgentTarget{}, false
}

// installBundledSkill replaces one skill through a same-directory staging path.
func installBundledSkill(
	skill bundledCodingAgentSkill, destination string, force bool,
) error {
	// Validate again at the mutation boundary in case a destination changed
	// after the command performed its all-skills preflight.
	if err := validateSkillDestination(destination, force); err != nil {
		return err
	}

	parent := filepath.Dir(destination)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return fmt.Errorf("create coding-agent skills directory: %w", err)
	}
	stagingRoot, err := os.MkdirTemp(parent, ".harnest-skill-install-")
	if err != nil {
		return fmt.Errorf("create coding-agent skill staging directory: %w", err)
	}
	defer os.RemoveAll(stagingRoot)
	staged := filepath.Join(stagingRoot, skill.name)
	if err := copyEmbeddedSkill(skill, staged); err != nil {
		return err
	}

	backup := filepath.Join(stagingRoot, "previous")
	hadPrevious := false
	if _, err := os.Lstat(destination); err == nil {
		hadPrevious = true
		if err := os.Rename(destination, backup); err != nil {
			return fmt.Errorf(
				"stage existing %s skill for replacement: %w", skill.name, err,
			)
		}
	}
	if err := os.Rename(staged, destination); err != nil {
		if hadPrevious {
			_ = os.Rename(backup, destination)
		}
		return fmt.Errorf("install %s skill: %w", skill.name, err)
	}
	return nil
}

func validateSkillDestination(destination string, force bool) error {
	info, err := os.Lstat(destination)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("inspect coding-agent skill destination: %w", err)
	}
	if !force {
		return fmt.Errorf(
			"coding-agent skill already exists at %s; pass --force to replace it",
			destination,
		)
	}
	if !info.IsDir() {
		return fmt.Errorf("coding-agent skill destination %s is not a directory", destination)
	}
	return nil
}

// copyEmbeddedSkill materializes one declared embedded tree into staging.
func copyEmbeddedSkill(skill bundledCodingAgentSkill, destination string) error {
	return fs.WalkDir(authoringSkill, skill.root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(skill.root, path)
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
