package engine

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"unicode/utf8"

	"gopkg.in/yaml.v3"
)

var (
	agentNamePattern             = regexp.MustCompile(`^[a-z][a-z0-9-]{0,62}$`)
	adkAgentNamePattern          = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,127}$`)
	cpuPattern                   = regexp.MustCompile(`^(?:[1-9][0-9]*m|[1-9][0-9]*(?:\.[0-9]+)?)$`)
	memoryPattern                = regexp.MustCompile(`^[1-9][0-9]*(?:Ki|Mi|Gi|Ti)$`)
	pythonPattern                = regexp.MustCompile(`^3\.(?:10|11|12|13|14)$`)
	entrypointPattern            = regexp.MustCompile(`^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):([A-Za-z_][A-Za-z0-9_]*)$`)
	environmentNamePattern       = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	secretEnvironmentNamePattern = regexp.MustCompile(`^[A-Z_][A-Z0-9_]*$`)
	compiledPluginVersionPattern = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$`)
)

func DecodePlan(reader io.Reader) (DeploymentPlan, error) {
	if reader == nil {
		return DeploymentPlan{}, fmt.Errorf("decode deployment plan: reader is nil")
	}
	var plan DeploymentPlan
	decoder := json.NewDecoder(reader)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&plan); err != nil {
		return DeploymentPlan{}, fmt.Errorf("decode deployment plan: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return DeploymentPlan{}, fmt.Errorf("decode deployment plan: multiple JSON values are not allowed")
		}
		return DeploymentPlan{}, fmt.Errorf("decode deployment plan: trailing data: %w", err)
	}
	if err := plan.Validate(); err != nil {
		return DeploymentPlan{}, err
	}
	return plan, nil
}

func (p DeploymentPlan) Validate() error {
	if p.APIVersion != APIVersion || p.Kind != "DeploymentPlan" {
		return fmt.Errorf("unsupported deployment plan %q/%q", p.APIVersion, p.Kind)
	}
	if strings.TrimSpace(p.ProjectRoot) == "" {
		return fmt.Errorf("deployment plan projectRoot is required")
	}
	if p.Parallelism < 1 {
		return fmt.Errorf("deployment plan parallelism must be at least 1")
	}
	if len(p.Sources) == 0 {
		return fmt.Errorf("deployment plan requires at least one source")
	}
	for sourceIndex, source := range p.Sources {
		if err := validatePlanSource(sourceIndex, source); err != nil {
			return err
		}
	}
	return nil
}

func validatePlanSource(index int, source AgentSource) error {
	if strings.TrimSpace(source.Root) == "" {
		return fmt.Errorf("deployment plan source %d root is required", index)
	}
	for _, patterns := range [][]string{source.Include, source.Exclude} {
		if err := validateSourcePatterns(index, patterns); err != nil {
			return err
		}
	}
	return nil
}

func validateSourcePatterns(index int, patterns []string) error {
	for _, pattern := range patterns {
		if pattern == "" {
			return fmt.Errorf("deployment plan source %d contains an empty pattern", index)
		}
		if filepath.Base(pattern) != pattern {
			return fmt.Errorf("deployment plan source %d pattern %q must match a direct child name", index, pattern)
		}
		if _, err := filepath.Match(pattern, "agent"); err != nil {
			return fmt.Errorf("deployment plan source %d has invalid pattern %q: %w", index, pattern, err)
		}
	}
	return nil
}

func LoadBundle(directory string) (Bundle, error) {
	directory, err := resolveBundleDirectory(directory)
	if err != nil {
		return Bundle{}, err
	}
	configPath, cardPath, instructionsPath, err := requiredBundlePaths(directory)
	if err != nil {
		return Bundle{}, err
	}
	if err := validateInstructions(instructionsPath); err != nil {
		return Bundle{}, err
	}
	if err := validateBundleResourceDirectories(directory); err != nil {
		return Bundle{}, err
	}

	var config AgentConfig
	if err := decodeYAMLFile(configPath, &config); err != nil {
		return Bundle{}, err
	}
	var card AgentCard
	if err := decodeYAMLFile(cardPath, &card); err != nil {
		return Bundle{}, err
	}
	if err := validateBundle(directory, config, card); err != nil {
		return Bundle{}, err
	}
	digest, err := digestDirectory(directory)
	if err != nil {
		return Bundle{}, err
	}
	return Bundle{
		Directory: directory, ConfigPath: configPath, CardPath: cardPath, InstructionsPath: instructionsPath,
		Config: config, Card: card, Digest: digest, LoadedAt: nowUTC(),
	}, nil
}

func resolveBundleDirectory(directory string) (string, error) {
	absolute, err := filepath.Abs(directory)
	if err != nil {
		return "", fmt.Errorf("resolve agent directory: %w", err)
	}
	resolved, err := filepath.EvalSymlinks(absolute)
	if err != nil {
		return "", fmt.Errorf("resolve agent directory symlinks: %w", err)
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return "", fmt.Errorf("stat agent directory: %w", err)
	}
	if !info.IsDir() {
		return "", fmt.Errorf("agent directory %s is not a directory", resolved)
	}
	return resolved, nil
}

func requiredBundlePaths(directory string) (string, string, string, error) {
	config, err := requiredBundleFile(directory, "config.yaml")
	if err != nil {
		return "", "", "", err
	}
	card, err := requiredBundleFile(directory, "agent-card.yaml")
	if err != nil {
		return "", "", "", err
	}
	instructions, err := requiredBundleFile(directory, "instructions.md")
	return config, card, instructions, err
}

func requiredBundleFile(directory, name string) (string, error) {
	resolved, err := containedRegularFile(directory, filepath.Join(directory, name))
	if err != nil {
		return "", fmt.Errorf("invalid %s: %w", name, err)
	}
	return resolved, nil
}

func validateInstructions(path string) error {
	contents, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read instructions.md: %w", err)
	}
	if !utf8.Valid(contents) {
		return fmt.Errorf("invalid instructions.md: content must be UTF-8")
	}
	if strings.TrimSpace(string(contents)) == "" {
		return fmt.Errorf("invalid instructions.md: content is empty")
	}
	return nil
}

// validateBundleResourceDirectories checks canonical and legacy roots before loading code.
func validateBundleResourceDirectories(directory string) error {
	legacy := filepath.Join(directory, "mcp_servers")
	if _, err := os.Lstat(legacy); err == nil {
		return fmt.Errorf("unsupported legacy MCP directory %s; use mcp", legacy)
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("inspect legacy MCP directory %s: %w", legacy, err)
	}
	for _, name := range []string{"lib", "tools", "tasks", "cron", "subagents", "mcp", "extensions", "lifecycle", "plugins", "sandbox", "skills", "evals"} {
		if err := validateOptionalBundleDirectory(directory, name); err != nil {
			return err
		}
	}
	return nil
}

func decodeYAMLFile(path string, target any) error {
	file, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("open %s: %w", path, err)
	}
	defer file.Close()
	decoder := yaml.NewDecoder(file)
	decoder.KnownFields(true)
	if err := decoder.Decode(target); err != nil {
		return fmt.Errorf("decode %s: %w", path, err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return fmt.Errorf("decode %s: multiple YAML documents are not allowed", path)
		}
		return fmt.Errorf("decode %s: trailing data: %w", path, err)
	}
	return nil
}

func validateBundle(directory string, config AgentConfig, card AgentCard) error {
	if err := validateAgentConfig(directory, config); err != nil {
		return err
	}
	return validateAgentCard(directory, card)
}

func validateAgentConfig(directory string, config AgentConfig) error {
	if err := validateConfigIdentity(directory, config); err != nil {
		return err
	}
	if err := validateFrameworkAndResources(directory, config); err != nil {
		return err
	}
	if err := validateDependencyProject(directory, config); err != nil {
		return err
	}
	for name := range config.Spec.Environment {
		if !environmentNamePattern.MatchString(name) {
			return fmt.Errorf("%s/config.yaml: invalid environment variable name %q", directory, name)
		}
	}
	return validateSecrets(directory, config)
}

func validateConfigIdentity(directory string, config AgentConfig) error {
	if config.APIVersion != APIVersion || config.Kind != "Agent" {
		return fmt.Errorf("%s/config.yaml: unsupported apiVersion/kind %q/%q", directory, config.APIVersion, config.Kind)
	}
	if !agentNamePattern.MatchString(config.Metadata.Name) {
		return fmt.Errorf("%s/config.yaml: metadata.name must be a lowercase DNS label", directory)
	}
	parts := entrypointPattern.FindStringSubmatch(config.Spec.Entrypoint)
	if parts == nil {
		return fmt.Errorf("%s/config.yaml: spec.entrypoint must use module:symbol syntax", directory)
	}
	modulePath := filepath.Join(directory, filepath.FromSlash(strings.ReplaceAll(parts[1], ".", "/"))+".py")
	if _, err := containedRegularFile(directory, modulePath); err != nil {
		return fmt.Errorf("%s/config.yaml: entrypoint module does not exist: %w", directory, err)
	}
	return nil
}

func validateDependencyProject(directory string, config AgentConfig) error {
	if config.Spec.Runtime.DependencyFile == "" {
		return fmt.Errorf("%s/config.yaml: dependencyFile is required", directory)
	}
	if config.Spec.Runtime.DependencyFile != "pyproject.toml" {
		return fmt.Errorf("%s/config.yaml: dependencyFile must be pyproject.toml", directory)
	}
	dependencyPath := filepath.Join(directory, config.Spec.Runtime.DependencyFile)
	if _, err := containedRegularFile(directory, dependencyPath); err != nil {
		return fmt.Errorf("%s/config.yaml: dependencyFile does not exist: %w", directory, err)
	}
	return nil
}

func validateFrameworkAndResources(directory string, config AgentConfig) error {
	if err := validateFrameworkRuntime(directory, config); err != nil {
		return err
	}
	if err := validateResourceQuantities(directory, config.Spec.Resources); err != nil {
		return err
	}
	return validateScaling(directory, config.Spec.Scaling)
}

func validateFrameworkRuntime(directory string, config AgentConfig) error {
	framework := config.Spec.Framework
	if framework.Name != "adk" && framework.Name != "langgraph" {
		return fmt.Errorf("%s/config.yaml: spec.framework.name must be adk or langgraph", directory)
	}
	if mode := framework.EffectiveMode(); mode != "managed" && mode != "advanced" {
		return fmt.Errorf("%s/config.yaml: spec.framework.mode must be managed or advanced", directory)
	}
	if !pythonPattern.MatchString(config.Spec.Runtime.Version) {
		return fmt.Errorf("%s/config.yaml: unsupported Python version %q", directory, config.Spec.Runtime.Version)
	}
	return nil
}

func validateResourceQuantities(directory string, resources AgentResources) error {
	if !cpuPattern.MatchString(resources.CPU) {
		return fmt.Errorf("%s/config.yaml: invalid CPU quantity %q", directory, resources.CPU)
	}
	if !memoryPattern.MatchString(resources.Memory) {
		return fmt.Errorf("%s/config.yaml: invalid memory quantity %q", directory, resources.Memory)
	}
	if resources.EphemeralStorage != "" && !memoryPattern.MatchString(resources.EphemeralStorage) {
		return fmt.Errorf("%s/config.yaml: invalid ephemeral storage quantity %q", directory, resources.EphemeralStorage)
	}
	if resources.TimeoutSeconds < 0 || resources.MaxConcurrentRequests < 0 {
		return fmt.Errorf("%s/config.yaml: resource limits cannot be negative", directory)
	}
	return nil
}

func validateScaling(directory string, scaling Scaling) error {
	if scaling.MinReplicas < 0 || scaling.MaxReplicas < 0 {
		return fmt.Errorf("%s/config.yaml: replica counts cannot be negative", directory)
	}
	if scaling.MaxReplicas > 0 && scaling.MinReplicas > scaling.MaxReplicas {
		return fmt.Errorf("%s/config.yaml: minReplicas cannot exceed maxReplicas", directory)
	}
	return nil
}

func validateSecrets(directory string, config AgentConfig) error {
	seenSecrets := make(map[string]struct{}, len(config.Spec.Secrets))
	for _, secret := range config.Spec.Secrets {
		if !secretEnvironmentNamePattern.MatchString(secret.EnvironmentVariable) || strings.TrimSpace(secret.SecretRef) == "" {
			return fmt.Errorf("%s/config.yaml: every secret needs a valid environmentVariable and secretRef", directory)
		}
		if _, exists := config.Spec.Environment[secret.EnvironmentVariable]; exists {
			return fmt.Errorf("%s/config.yaml: environment variable %q has both a literal and secret value", directory, secret.EnvironmentVariable)
		}
		if _, exists := seenSecrets[secret.EnvironmentVariable]; exists {
			return fmt.Errorf("%s/config.yaml: duplicate secret environment variable %q", directory, secret.EnvironmentVariable)
		}
		seenSecrets[secret.EnvironmentVariable] = struct{}{}
	}
	return nil
}

func validateAgentCard(directory string, card AgentCard) error {
	if card.Name == "" || card.Description == "" || card.Version == "" {
		return fmt.Errorf("%s/agent-card.yaml: name, description, and version are required", directory)
	}
	if len(card.SupportedInterfaces) == 0 || len(card.DefaultInputModes) == 0 || len(card.DefaultOutputModes) == 0 || len(card.Skills) == 0 {
		return fmt.Errorf("%s/agent-card.yaml: supportedInterfaces, default modes, and skills are required", directory)
	}
	if err := validateSupportedInterfaces(directory, card.SupportedInterfaces); err != nil {
		return err
	}
	if err := validateDefaultModes(directory, card); err != nil {
		return err
	}
	return validateCardSkills(directory, card.Skills)
}

func validateSupportedInterfaces(directory string, interfaces []AgentInterface) error {
	for _, supportedInterface := range interfaces {
		if strings.TrimSpace(supportedInterface.ProtocolBinding) == "" || strings.TrimSpace(supportedInterface.ProtocolVersion) == "" {
			return fmt.Errorf("%s/agent-card.yaml: every supported interface needs url, protocolBinding, and protocolVersion", directory)
		}
		parsedURL, err := url.ParseRequestURI(supportedInterface.URL)
		if err != nil || parsedURL.Scheme == "" || parsedURL.Host == "" {
			return fmt.Errorf("%s/agent-card.yaml: invalid supported interface URL %q", directory, supportedInterface.URL)
		}
	}
	return nil
}

func validateDefaultModes(directory string, card AgentCard) error {
	for _, modes := range [][]string{card.DefaultInputModes, card.DefaultOutputModes} {
		for _, mode := range modes {
			if strings.TrimSpace(mode) == "" {
				return fmt.Errorf("%s/agent-card.yaml: default modes cannot contain empty values", directory)
			}
		}
	}
	return nil
}

func validateCardSkills(directory string, skills []AgentSkill) error {
	seenSkills := make(map[string]struct{}, len(skills))
	for _, skill := range skills {
		if skill.ID == "" || skill.Name == "" || skill.Description == "" || len(skill.Tags) == 0 {
			return fmt.Errorf("%s/agent-card.yaml: every skill needs id, name, description, and tags", directory)
		}
		if _, exists := seenSkills[skill.ID]; exists {
			return fmt.Errorf("%s/agent-card.yaml: duplicate skill id %q", directory, skill.ID)
		}
		seenSkills[skill.ID] = struct{}{}
	}
	return nil
}

func containedRegularFile(root, candidate string) (string, error) {
	if !within(root, candidate) {
		return "", fmt.Errorf("path %s escapes %s", candidate, root)
	}
	unresolvedInfo, err := os.Lstat(candidate)
	if err != nil {
		return "", err
	}
	if !unresolvedInfo.Mode().IsRegular() {
		return "", fmt.Errorf("path %s is not a regular file", candidate)
	}
	resolved, err := filepath.EvalSymlinks(candidate)
	if err != nil {
		return "", err
	}
	if !within(root, resolved) {
		return "", fmt.Errorf("path %s resolves outside %s", candidate, root)
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("path %s is not a regular file", candidate)
	}
	return resolved, nil
}

func validateOptionalBundleDirectory(root, name string) error {
	directory := filepath.Join(root, name)
	info, err := os.Lstat(directory)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return fmt.Errorf("inspect optional bundle directory %s: %w", directory, err)
	}
	if !info.IsDir() {
		return fmt.Errorf("optional bundle path %s must be a directory, not a symlink or file", directory)
	}
	return filepath.WalkDir(directory, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return fmt.Errorf("inspect bundle resource %s: %w", path, walkErr)
		}
		if path == directory || entry.IsDir() {
			return nil
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("bundle resource %s must not be a symlink", path)
		}
		entryInfo, err := entry.Info()
		if err != nil {
			return fmt.Errorf("inspect bundle resource %s: %w", path, err)
		}
		if !entryInfo.Mode().IsRegular() {
			return fmt.Errorf("bundle resource %s must be a regular file", path)
		}
		return nil
	})
}

func within(root, candidate string) bool {
	relative, err := filepath.Rel(root, candidate)
	return err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator))
}
