package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"github.com/pelletier/go-toml/v2"
	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"
)

const maxExtensionManifestBytes = 1024 * 1024

var extensionSemver = regexp.MustCompile(
	`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)` +
		`(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)` +
		`(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?` +
		`(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$`,
)

var pythonKeywords = map[string]struct{}{
	"False": {}, "None": {}, "True": {}, "and": {}, "as": {}, "assert": {},
	"async": {}, "await": {}, "break": {}, "class": {}, "continue": {}, "def": {},
	"del": {}, "elif": {}, "else": {}, "except": {}, "finally": {}, "for": {},
	"from": {}, "global": {}, "if": {}, "import": {}, "in": {}, "is": {},
	"lambda": {}, "nonlocal": {}, "not": {}, "or": {}, "pass": {}, "raise": {},
	"return": {}, "try": {}, "while": {}, "with": {}, "yield": {},
}

var extensionCapabilities = map[string]struct{}{
	"context.assets": {}, "context.credentials": {}, "context.continuations": {},
	"context.mcp": {}, "context.resources": {}, "context.session": {},
	"context.skills": {}, "context.storage": {}, "content.mcp": {},
	"content.skills": {}, "content.subagents": {}, "content.tools": {},
	"http.routes": {}, "lifecycle.agent": {}, "lifecycle.http": {},
	"lifecycle.mcp": {}, "lifecycle.model": {}, "lifecycle.skills": {},
	"lifecycle.tool": {}, "native.adk": {}, "native.langgraph": {},
	"policy.output": {}, "sandbox.provider": {}, "storage.assets": {},
	"storage.checkpoints": {}, "storage.custom": {}, "storage.sessions": {},
	"storage.tasks": {}, "storage.cron": {}, "storage.memory": {}, "telemetry.exporter": {},
}

var extensionRootEntries = map[string]bool{
	"README.md":      true,
	"extension.yaml": true,
	"extension.py":   true,
	"pyproject.toml": true,
	"lifecycle":      false,
	"lib":            false,
	"mcp":            false,
	"skills":         false,
	"subagents":      false,
	"tools":          false,
}

var extensionContributionKinds = []string{"lifecycle", "mcp", "skills", "subagents", "tools"}

var extensionContributionPath = regexp.MustCompile(
	`^[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*/?$`,
)

type localExtensionContributions struct {
	Lifecycle *[]string `yaml:"lifecycle,omitempty"`
	MCP       *[]string `yaml:"mcp,omitempty"`
	Skills    *[]string `yaml:"skills,omitempty"`
	Subagents *[]string `yaml:"subagents,omitempty"`
	Tools     *[]string `yaml:"tools,omitempty"`
}

type localExtensionManifest struct {
	APIVersion string `yaml:"apiVersion"`
	Kind       string `yaml:"kind"`
	Metadata   *struct {
		Name    string `yaml:"name"`
		Version string `yaml:"version"`
	} `yaml:"metadata"`
	Runtime *struct {
		Entrypoint string `yaml:"entrypoint"`
	} `yaml:"runtime"`
	Requires *struct {
		Extensions []string `yaml:"extensions"`
	} `yaml:"requires,omitempty"`
	Contributes  *localExtensionContributions `yaml:"contributes,omitempty"`
	Capabilities *[]string                    `yaml:"capabilities,omitempty"`
}

type installedExtension struct {
	Name string
	Path string
}

// installExtension keeps local-path compatibility while treating bare names as PyPI projects.
func (a *application) installExtension(
	ctx context.Context, source, project string, force bool,
) (installedExtension, error) {
	if localExtensionInstallSource(source) {
		return installLocalExtension(source, project, force)
	}
	return a.installPyPIExtension(ctx, source, project, force)
}

// localExtensionInstallSource prevents a missing path from becoming an unintended network lookup.
func localExtensionInstallSource(source string) bool {
	_, err := os.Lstat(source)
	return err == nil || !os.IsNotExist(err) || filepath.IsAbs(source) ||
		strings.ContainsAny(source, `/\`) || strings.HasPrefix(source, ".")
}

// newExtensionInitCommand creates one application-local canonical package.
func (a *application) newExtensionInitCommand() *cobra.Command {
	var project string
	var capabilities []string
	command := &cobra.Command{
		Use:   "init NAME",
		Short: "Initialize an application-local Harnest Extension",
		Args:  cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			created, err := initializeLocalExtension(arguments[0], project, capabilities)
			if err != nil {
				return err
			}
			fmt.Fprintf(command.OutOrStdout(), "Initialized Harnest Extension %q at %s\n", created.Name, created.Path)
			fmt.Fprintf(command.OutOrStdout(), "Run `harnest env sync %s` to refresh harnest-runtime.lock.\n", extensionProjectRoot(created.Path))
			return nil
		},
	}
	command.Flags().StringVar(
		&project, "project", ".", "Harnest agent root containing config.yaml",
	)
	command.Flags().StringSliceVar(
		&capabilities, "capability", nil,
		"declared extension capability (repeatable, for example sandbox.provider)",
	)
	return command
}

// newExtensionInstallCommand installs a verified local or PyPI package without importing it.
func (a *application) newExtensionInstallCommand() *cobra.Command {
	var project string
	var force bool
	command := &cobra.Command{
		Use:   "install SOURCE",
		Short: "Install a local or PyPI Harnest Extension",
		Long: `Install a Harnest Extension from a local directory, a complete PyPI
project name such as harnest-extension-docker, or its short slug such as docker.

PyPI installs verify the published wheel digest and extension contract without
executing package code. Harnest materializes that exact release and preserves
its dependency metadata; run harnest env sync afterwards to lock the agent
environment.`,
		Args: cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			installed, err := a.installExtension(
				command.Context(), arguments[0], project, force,
			)
			if err != nil {
				return err
			}
			fmt.Fprintf(command.OutOrStdout(), "Installed Harnest Extension %q at %s\n", installed.Name, installed.Path)
			fmt.Fprintf(command.OutOrStdout(), "Run `harnest env sync %s` to refresh harnest-runtime.lock.\n", extensionProjectRoot(installed.Path))
			return nil
		},
	}
	command.Flags().StringVar(
		&project, "project", ".", "Harnest agent root containing config.yaml",
	)
	command.Flags().BoolVar(
		&force, "force", false, "replace an existing Harnest Extension with the same manifest name",
	)
	return command
}

// extensionProjectRoot derives the validated agent root from an installed package path.
func extensionProjectRoot(extensionPath string) string {
	return filepath.Dir(filepath.Dir(extensionPath))
}

// initializeLocalExtension validates identity and authority before creating files.
func initializeLocalExtension(name, project string, capabilities []string) (installedExtension, error) {
	if err := validateExtensionName(name); err != nil {
		return installedExtension{}, err
	}
	validatedCapabilities, err := validateExtensionCapabilities(capabilities)
	if err != nil {
		return installedExtension{}, err
	}
	projectRoot, err := validatedAgentProject(project)
	if err != nil {
		return installedExtension{}, err
	}
	extensionsRoot := filepath.Join(projectRoot, "extensions")
	if err := validateLocalPackageRoot(extensionsRoot, "extensions"); err != nil {
		return installedExtension{}, err
	}
	destination := filepath.Join(extensionsRoot, name)
	if err := createExtensionScaffold(destination, name, validatedCapabilities); err != nil {
		return installedExtension{}, err
	}
	return installedExtension{Name: name, Path: destination}, nil
}

// createExtensionScaffold stages both required files before publishing the package.
func createExtensionScaffold(destination, name string, capabilities []string) error {
	if err := validateLocalPackageDestination(destination, false, "Harnest Extension"); err != nil {
		return err
	}
	parent := filepath.Dir(destination)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return fmt.Errorf("create target extensions directory: %w", err)
	}
	stagingRoot, err := os.MkdirTemp(parent, ".harnest-extension-init-")
	if err != nil {
		return fmt.Errorf("create Harnest Extension staging directory: %w", err)
	}
	defer os.RemoveAll(stagingRoot)
	staged := filepath.Join(stagingRoot, name)
	if err := os.Mkdir(staged, 0o755); err != nil {
		return fmt.Errorf("create staged Harnest Extension: %w", err)
	}
	files := map[string]string{
		"extension.yaml": extensionManifestScaffold(name, capabilities),
		"extension.py":   extensionPythonScaffold(name),
		"pyproject.toml": extensionProjectScaffold(name),
	}
	for filename, contents := range files {
		if err := os.WriteFile(filepath.Join(staged, filename), []byte(contents), 0o644); err != nil {
			return fmt.Errorf("write staged Harnest Extension %s: %w", filename, err)
		}
	}
	// Recheck after staging so concurrent creation never gets silently replaced.
	if err := validateLocalPackageDestination(destination, false, "Harnest Extension"); err != nil {
		return err
	}
	if err := os.Rename(staged, destination); err != nil {
		return fmt.Errorf("initialize Harnest Extension: %w", err)
	}
	return nil
}

// extensionManifestScaffold emits the canonical descriptor and selected authority only.
func extensionManifestScaffold(name string, capabilities []string) string {
	var content strings.Builder
	fmt.Fprintf(&content, "apiVersion: harnest.dev/v1alpha1\nkind: Extension\nmetadata:\n  name: %s\n  version: 0.1.0\nruntime:\n  entrypoint: extension:extension\ncapabilities:", name)
	if len(capabilities) == 0 {
		content.WriteString(" []\n")
		return content.String()
	}
	content.WriteString("\n")
	for _, capability := range capabilities {
		fmt.Fprintf(&content, "  - %s\n", capability)
	}
	return content.String()
}

// extensionProjectScaffold keeps provider SDK requirements in the root environment solve.
func extensionProjectScaffold(name string) string {
	return fmt.Sprintf(`[project]
name = "harnest-extension-%s"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = []

[tool.uv]
package = false
`, strings.ReplaceAll(name, "_", "-"))
}

// extensionPythonScaffold gives the package a local public class and singleton.
func extensionPythonScaffold(name string) string {
	className := extensionClassName(name)
	return fmt.Sprintf(`"""Application-local %s Harnest Extension."""

from harnest.extensions import Extension


class %s(Extension):
    """Own application-scoped behavior without importing a framework."""


extension = %s()
`, name, className, className)
}

// extensionClassName maps snake-case package identity to a readable Python type.
func extensionClassName(name string) string {
	parts := strings.FieldsFunc(name, func(character rune) bool { return character == '_' })
	for index, part := range parts {
		parts[index] = strings.ToUpper(part[:1]) + part[1:]
	}
	className := strings.Join(parts, "") + "Extension"
	if strings.HasPrefix(name, "_") {
		return "Local" + className
	}
	return className
}

// installLocalExtension validates the executable package before entering mutation.
func installLocalExtension(source, project string, force bool) (installedExtension, error) {
	sourceRoot, err := validatedLocalPackageSource(source, "Harnest Extension")
	if err != nil {
		return installedExtension{}, err
	}
	manifest, err := readLocalExtensionManifest(sourceRoot)
	if err != nil {
		return installedExtension{}, err
	}
	if err := validateLocalExtensionLayout(sourceRoot, manifest); err != nil {
		return installedExtension{}, err
	}
	if err := validateLocalExtensionProject(sourceRoot, manifest); err != nil {
		return installedExtension{}, err
	}
	projectRoot, err := validatedAgentProject(project)
	if err != nil {
		return installedExtension{}, err
	}
	extensionsRoot := filepath.Join(projectRoot, "extensions")
	if err := validateLocalPackageRoot(extensionsRoot, "extensions"); err != nil {
		return installedExtension{}, err
	}
	if pathContains(sourceRoot, extensionsRoot) {
		return installedExtension{}, fmt.Errorf("extension source cannot contain the target extensions directory")
	}
	destination := filepath.Join(extensionsRoot, manifest.Metadata.Name)
	if err := installLocalPackageTree(
		sourceRoot, destination, force, "Harnest Extension", "extensions",
	); err != nil {
		return installedExtension{}, err
	}
	return installedExtension{Name: manifest.Metadata.Name, Path: destination}, nil
}

// validateLocalExtensionLayout mirrors the compiler's closed top-level boundary.
func validateLocalExtensionLayout(root string, manifest localExtensionManifest) error {
	allowed := extensionAllowedRootEntries(manifest)
	entries, err := os.ReadDir(root)
	if err != nil {
		return fmt.Errorf("read Harnest Extension source: %w", err)
	}
	for _, entry := range entries {
		path := filepath.Join(root, entry.Name())
		info, err := inspectLocalExtensionRootEntry(path, entry)
		if err != nil {
			return err
		}
		if ignoredExtensionRootEntry(entry.Name()) {
			continue
		}
		expectsFile, found := allowed[entry.Name()]
		if !found {
			return fmt.Errorf("unexpected Harnest Extension resource: %s", path)
		}
		if expectsFile != info.Mode().IsRegular() {
			expected := "directory"
			if expectsFile {
				expected = "file"
			}
			return fmt.Errorf("Harnest Extension resource must be a %s: %s", expected, path)
		}
	}
	return validateLocalContributionDirectories(root, manifest)
}

// extensionContributionEntries returns fixed kinds in deterministic manifest order.
func extensionContributionEntries(manifest localExtensionManifest) map[string][]string {
	if manifest.Contributes == nil {
		return nil
	}
	entries := map[string][]string{}
	values := map[string]*[]string{
		"lifecycle": manifest.Contributes.Lifecycle,
		"mcp":       manifest.Contributes.MCP,
		"skills":    manifest.Contributes.Skills,
		"subagents": manifest.Contributes.Subagents,
		"tools":     manifest.Contributes.Tools,
	}
	for kind, paths := range values {
		if paths != nil {
			entries[kind] = *paths
		}
	}
	return entries
}

// extensionAllowedRootEntries admits only core files and explicitly declared roots.
func extensionAllowedRootEntries(manifest localExtensionManifest) map[string]bool {
	allowed := make(map[string]bool, len(extensionRootEntries)+len(extensionContributionKinds))
	for name, expectsFile := range extensionRootEntries {
		allowed[name] = expectsFile
	}
	for _, values := range extensionContributionEntries(manifest) {
		for _, value := range values {
			root, _, _ := strings.Cut(strings.TrimSuffix(value, "/"), "/")
			allowed[root] = false
		}
	}
	return allowed
}

// validateLocalContributionDirectories binds declarations to contained directories.
func validateLocalContributionDirectories(root string, manifest localExtensionManifest) error {
	entries := extensionContributionEntries(manifest)
	if err := validateDeclaredContributionDirectories(root, entries); err != nil {
		return err
	}
	return validateConventionalContributionDirectories(root, entries)
}

// validateDeclaredContributionDirectories requires every projected source directory.
func validateDeclaredContributionDirectories(root string, entries map[string][]string) error {
	for _, kind := range extensionContributionKinds {
		values := entries[kind]
		for _, value := range values {
			target := filepath.Join(root, filepath.FromSlash(strings.TrimSuffix(value, "/")))
			info, err := os.Lstat(target)
			if err != nil || !info.IsDir() {
				return fmt.Errorf("contributes.%s path must be an existing regular directory: %q", kind, value)
			}
		}
	}
	return nil
}

// validateConventionalContributionDirectories rejects formerly inferred content.
func validateConventionalContributionDirectories(root string, entries map[string][]string) error {
	for _, kind := range extensionContributionKinds {
		conventional := filepath.Join(root, kind)
		info, err := os.Lstat(conventional)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil || !info.IsDir() {
			continue
		}
		populated, err := hasPublicExtensionEntries(conventional)
		if err != nil {
			return err
		}
		if populated && !declaresExtensionPath(entries[kind], kind) {
			return fmt.Errorf("Harnest Extension content directory %s must be declared under contributes.%s", conventional, kind)
		}
	}
	return nil
}

// hasPublicExtensionEntries ignores placeholders and local state like the compiler.
func hasPublicExtensionEntries(directory string) (bool, error) {
	entries, err := os.ReadDir(directory)
	if err != nil {
		return false, fmt.Errorf("read Harnest Extension content directory %s: %w", directory, err)
	}
	for _, entry := range entries {
		if !ignoredExtensionRootEntry(entry.Name()) {
			return true, nil
		}
	}
	return false, nil
}

// declaresExtensionPath reports whether one declaration projects a conventional root.
func declaresExtensionPath(values []string, root string) bool {
	for _, value := range values {
		canonical := strings.TrimSuffix(value, "/")
		if canonical == root || strings.HasPrefix(canonical, root+"/") {
			return true
		}
	}
	return false
}

// inspectLocalExtensionRootEntry validates kind before exclusions can hide links or devices.
func inspectLocalExtensionRootEntry(path string, entry fs.DirEntry) (fs.FileInfo, error) {
	info, err := entry.Info()
	if err != nil {
		return nil, fmt.Errorf("inspect Harnest Extension resource %s: %w", path, err)
	}
	if info.Mode()&os.ModeSymlink != 0 || (!info.IsDir() && !info.Mode().IsRegular()) {
		return nil, fmt.Errorf("Harnest Extension resources must be regular files or directories: %s", path)
	}
	return info, nil
}

// ignoredExtensionRootEntry matches compiler-owned guide and local-state exclusions.
func ignoredExtensionRootEntry(name string) bool {
	return strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_")
}

// validateLocalExtensionProject binds optional dependency metadata to the manifest.
func validateLocalExtensionProject(root string, manifest localExtensionManifest) error {
	path := filepath.Join(root, "pyproject.toml")
	project, err := readLocalExtensionProject(path)
	if err != nil || project == nil {
		return err
	}
	name, nameOK := project["name"].(string)
	version, versionOK := project["version"].(string)
	manifestName := manifest.Metadata.Name
	wantedName := normalizeProjectName("harnest-extension-" + manifestName)
	if !nameOK || normalizeProjectName(name) != wantedName {
		return fmt.Errorf("Harnest Extension pyproject name must be %q", "harnest-extension-"+strings.ReplaceAll(manifestName, "_", "-"))
	}
	if !versionOK || version != manifest.Metadata.Version {
		return fmt.Errorf("Harnest Extension pyproject version must match extension.yaml metadata.version")
	}
	return nil
}

// readLocalExtensionProject decodes one bounded optional PEP 621 project table.
func readLocalExtensionProject(path string) (map[string]any, error) {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil || !info.Mode().IsRegular() {
		return nil, fmt.Errorf("Harnest Extension pyproject.toml must be a regular file: %s", path)
	}
	if info.Size() > maxExtensionManifestBytes {
		return nil, fmt.Errorf("Harnest Extension pyproject.toml exceeds the 1 MiB limit")
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read Harnest Extension pyproject.toml: %w", err)
	}
	var document map[string]any
	if err := toml.Unmarshal(contents, &document); err != nil {
		return nil, fmt.Errorf("invalid Harnest Extension pyproject.toml: %w", err)
	}
	project, ok := document["project"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("Harnest Extension pyproject.toml must define a PEP 621 [project] table")
	}
	return project, nil
}

// readLocalExtensionManifest applies the closed canonical descriptor without imports.
func readLocalExtensionManifest(root string) (localExtensionManifest, error) {
	path := filepath.Join(root, "extension.yaml")
	contents, err := readRegularExtensionFile(path, maxExtensionManifestBytes, "manifest")
	if err != nil {
		return localExtensionManifest{}, err
	}
	manifest, err := decodeLocalExtensionManifest(contents)
	if err != nil {
		return localExtensionManifest{}, fmt.Errorf("invalid extension.yaml: %w", err)
	}
	if err := validateLocalExtensionManifest(manifest); err != nil {
		return localExtensionManifest{}, fmt.Errorf("invalid extension.yaml: %w", err)
	}
	if _, err := readRegularExtensionFile(filepath.Join(root, "extension.py"), -1, "entrypoint"); err != nil {
		return localExtensionManifest{}, err
	}
	return manifest, nil
}

// readRegularExtensionFile rejects missing, linked, special, and oversized contract files.
func readRegularExtensionFile(path string, maximum int64, label string) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, fmt.Errorf("read Harnest Extension %s: %w", label, err)
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("Harnest Extension %s must be a regular file: %s", label, path)
	}
	if maximum >= 0 && info.Size() > maximum {
		return nil, fmt.Errorf("Harnest Extension %s exceeds the 1 MiB limit", label)
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read Harnest Extension %s: %w", label, err)
	}
	return contents, nil
}

// decodeLocalExtensionManifest rejects unknown fields and multiple YAML documents.
func decodeLocalExtensionManifest(contents []byte) (localExtensionManifest, error) {
	var manifest localExtensionManifest
	decoder := yaml.NewDecoder(bytes.NewReader(contents))
	decoder.KnownFields(true)
	if err := decoder.Decode(&manifest); err != nil {
		return localExtensionManifest{}, err
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		if err == nil {
			return localExtensionManifest{}, fmt.Errorf("manifest must contain exactly one YAML document")
		}
		return localExtensionManifest{}, err
	}
	return manifest, nil
}

// validateLocalExtensionManifest binds identity, entrypoint, dependencies, and authority.
func validateLocalExtensionManifest(manifest localExtensionManifest) error {
	if err := validateLocalExtensionIdentity(manifest); err != nil {
		return err
	}
	if manifest.Requires != nil {
		if err := validateExtensionDependencies(manifest.Requires.Extensions); err != nil {
			return err
		}
	}
	if err := validateExtensionContributions(manifest); err != nil {
		return err
	}
	if manifest.Capabilities != nil {
		_, err := validateExtensionCapabilities(*manifest.Capabilities)
		return err
	}
	return nil
}

// validateExtensionContributions rejects ambiguous or escaping content projections.
func validateExtensionContributions(manifest localExtensionManifest) error {
	entries := extensionContributionEntries(manifest)
	if manifest.Contributes != nil && !hasExtensionContributions(entries) {
		return fmt.Errorf("contributes must declare at least one contribution path")
	}
	declared := map[string]string{}
	capabilities := extensionCapabilitySet(manifest.Capabilities)
	for _, kind := range extensionContributionKinds {
		if err := validateExtensionContributionKind(
			kind, entries, capabilities, declared,
		); err != nil {
			return err
		}
	}
	return nil
}

// extensionCapabilitySet normalizes optional authority for contribution checks.
func extensionCapabilitySet(values *[]string) map[string]struct{} {
	result := map[string]struct{}{}
	if values == nil {
		return result
	}
	for _, value := range *values {
		result[value] = struct{}{}
	}
	return result
}

// validateExtensionContributionKind validates one projection list against shared state.
func validateExtensionContributionKind(
	kind string, entries map[string][]string, capabilities map[string]struct{}, declared map[string]string,
) error {
	values, present := entries[kind]
	if present && len(values) == 0 {
		return fmt.Errorf("contributes.%s must contain at least one path", kind)
	}
	if err := requireExtensionContentCapability(kind, present, capabilities); err != nil {
		return err
	}
	seen := map[string]struct{}{}
	for _, value := range values {
		canonical, err := validateExtensionContributionPath(kind, value)
		if err != nil {
			return err
		}
		if _, duplicate := seen[canonical]; duplicate {
			return fmt.Errorf("duplicate Harnest Extension contributes.%s path %q", kind, canonical)
		}
		seen[canonical] = struct{}{}
		if err := registerExtensionContributionPath(kind, canonical, declared); err != nil {
			return err
		}
	}
	return nil
}

// requireExtensionContentCapability keeps projection and authority independent.
func requireExtensionContentCapability(kind string, present bool, capabilities map[string]struct{}) error {
	required := extensionContentCapability(kind)
	if required == "" || !present {
		return nil
	}
	if _, declared := capabilities[required]; declared {
		return nil
	}
	return fmt.Errorf("contributes.%s requires Harnest Extension capability %q", kind, required)
}

// registerExtensionContributionPath rejects overlap across every contribution kind.
func registerExtensionContributionPath(kind, canonical string, declared map[string]string) error {
	previousPaths := make([]string, 0, len(declared))
	for previous := range declared {
		previousPaths = append(previousPaths, previous)
	}
	sort.Strings(previousPaths)
	for _, previous := range previousPaths {
		if overlappingExtensionPaths(canonical, previous) {
			return fmt.Errorf("Harnest Extension contribution paths overlap: contributes.%s=%q and contributes.%s=%q", declared[previous], previous, kind, canonical)
		}
	}
	declared[canonical] = kind
	return nil
}

// extensionContentCapability keeps file contribution authority separate from paths.
func extensionContentCapability(kind string) string {
	switch kind {
	case "mcp":
		return "content.mcp"
	case "skills":
		return "content.skills"
	case "subagents":
		return "content.subagents"
	case "tools":
		return "content.tools"
	default:
		return ""
	}
}

// hasExtensionContributions distinguishes an absent declaration from an empty mapping.
func hasExtensionContributions(entries map[string][]string) bool {
	for _, values := range entries {
		if len(values) > 0 {
			return true
		}
	}
	return false
}

// validateExtensionContributionPath canonicalizes one portable relative directory.
func validateExtensionContributionPath(kind, value string) (string, error) {
	canonical := strings.TrimSuffix(value, "/")
	root, _, _ := strings.Cut(canonical, "/")
	_, reserved := extensionRootEntries[root]
	if !extensionContributionPath.MatchString(value) || path.Clean(canonical) != canonical || reserved && (extensionRootEntries[root] || root == "lib") {
		return "", fmt.Errorf("contributes.%s path must be a safe package-relative directory: %q", kind, value)
	}
	return canonical, nil
}

// overlappingExtensionPaths prevents one tree from being interpreted as two contracts.
func overlappingExtensionPaths(left, right string) bool {
	return left == right || strings.HasPrefix(left, right+"/") || strings.HasPrefix(right, left+"/")
}

// validateLocalExtensionIdentity checks the canonical descriptor discriminators.
func validateLocalExtensionIdentity(manifest localExtensionManifest) error {
	if manifest.APIVersion != "harnest.dev/v1alpha1" {
		return fmt.Errorf("apiVersion must be harnest.dev/v1alpha1")
	}
	if manifest.Kind != "Extension" {
		return fmt.Errorf("kind must be Extension")
	}
	if manifest.Metadata == nil {
		return fmt.Errorf("metadata must be a mapping")
	}
	if err := validateExtensionName(manifest.Metadata.Name); err != nil {
		return err
	}
	if !extensionSemver.MatchString(manifest.Metadata.Version) {
		return fmt.Errorf("metadata.version must be a valid semantic version")
	}
	if manifest.Runtime == nil || manifest.Runtime.Entrypoint != "extension:extension" {
		return fmt.Errorf("runtime.entrypoint must be extension:extension")
	}
	return nil
}

// validateExtensionName requires the portable Python package identity contract.
func validateExtensionName(name string) error {
	_, keyword := pythonKeywords[name]
	if !validPythonIdentifier(name) || keyword {
		return fmt.Errorf("extension name must be a non-keyword Python identifier")
	}
	return nil
}

// validateExtensionDependencies rejects ambiguous or invalid dependency identity.
func validateExtensionDependencies(names []string) error {
	seen := make(map[string]struct{}, len(names))
	for _, name := range names {
		if err := validateExtensionName(name); err != nil {
			return fmt.Errorf("requires.extensions contains invalid name %q", name)
		}
		if _, duplicate := seen[name]; duplicate {
			return fmt.Errorf("duplicate Harnest Extension dependencies: %s", name)
		}
		seen[name] = struct{}{}
	}
	return nil
}

// validateExtensionCapabilities enforces the closed authority vocabulary.
func validateExtensionCapabilities(values []string) ([]string, error) {
	result := append([]string(nil), values...)
	seen := make(map[string]struct{}, len(result))
	for _, capability := range result {
		if _, known := extensionCapabilities[capability]; !known {
			return nil, fmt.Errorf("unknown Harnest Extension capability %q", capability)
		}
		if _, duplicate := seen[capability]; duplicate {
			return nil, fmt.Errorf("duplicate Harnest Extension capability %q", capability)
		}
		seen[capability] = struct{}{}
	}
	sort.Strings(result)
	return result, nil
}
