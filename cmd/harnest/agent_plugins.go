package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/spf13/cobra"
)

const (
	agentPluginSchema       = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
	maxAgentPluginJSONBytes = 1024 * 1024
)

var agentPluginName = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$`)

type agentPluginManifest struct {
	Name string
}

// newAgentPluginsCommand owns portable Agent Plugins separately from executable Extensions.
func (a *application) newAgentPluginsCommand() *cobra.Command {
	command := &cobra.Command{
		Use:   "plugins",
		Short: "Install portable Agent Plugins into an agent",
		Long: `Install portable Agent Plugins 1.0 without importing package code.

The install command accepts a local plugin directory containing plugin.json and
copies it to plugins/<manifest-name> in the selected Harnest agent. Harnest
Extensions are executable Python packages and use the separate extensions
command namespace.`,
	}
	command.AddCommand(a.newAgentPluginInstallCommand())
	return command
}

// newAgentPluginInstallCommand validates and atomically installs one local package.
func (a *application) newAgentPluginInstallCommand() *cobra.Command {
	var project string
	var force bool
	command := &cobra.Command{
		Use:   "install SOURCE",
		Short: "Install a local Agent Plugins 1.0 directory",
		Args:  cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			installed, err := installAgentPlugin(arguments[0], project, force)
			if err != nil {
				return err
			}
			fmt.Fprintf(command.OutOrStdout(), "Installed Agent Plugin %q at %s\n", installed.Name, installed.Path)
			return nil
		},
	}
	command.Flags().StringVar(
		&project, "project", ".", "Harnest agent root containing config.yaml",
	)
	command.Flags().BoolVar(
		&force, "force", false, "replace an existing Agent Plugin with the same manifest name",
	)
	return command
}

type installedAgentPlugin struct {
	Name string
	Path string
}

// installAgentPlugin validates source and destination before entering the mutation boundary.
func installAgentPlugin(source, project string, force bool) (installedAgentPlugin, error) {
	sourceRoot, err := validatedAgentPluginSource(source)
	if err != nil {
		return installedAgentPlugin{}, err
	}
	manifest, err := readAgentPluginManifest(sourceRoot)
	if err != nil {
		return installedAgentPlugin{}, err
	}
	projectRoot, err := validatedAgentProject(project)
	if err != nil {
		return installedAgentPlugin{}, err
	}
	pluginsRoot := filepath.Join(projectRoot, "plugins")
	if err := validateAgentPluginsRoot(pluginsRoot); err != nil {
		return installedAgentPlugin{}, err
	}
	if pathContains(sourceRoot, pluginsRoot) {
		return installedAgentPlugin{}, fmt.Errorf("plugin source cannot contain the target plugins directory")
	}
	destination := filepath.Join(pluginsRoot, manifest.Name)
	if err := installAgentPluginTree(sourceRoot, destination, force); err != nil {
		return installedAgentPlugin{}, err
	}
	return installedAgentPlugin{Name: manifest.Name, Path: destination}, nil
}

// validatedAgentPluginSource resolves a user-selected directory and rejects linked roots.
func validatedAgentPluginSource(source string) (string, error) {
	return validatedLocalPackageSource(source, "Agent Plugin")
}

// rejectAgentPluginTreeLinks permits only ordinary directories and regular files.
func rejectAgentPluginTreeLinks(root string) error {
	return rejectLocalPackageTreeLinks(root, "Agent Plugin")
}

// readAgentPluginManifest applies the local Agent Plugins 1.0 manifest contract.
func readAgentPluginManifest(root string) (agentPluginManifest, error) {
	path := filepath.Join(root, "plugin.json")
	info, err := os.Lstat(path)
	if err != nil {
		return agentPluginManifest{}, fmt.Errorf("read Agent Plugin manifest: %w", err)
	}
	if !info.Mode().IsRegular() {
		return agentPluginManifest{}, fmt.Errorf("Agent Plugin manifest must be a regular file: %s", path)
	}
	if info.Size() > maxAgentPluginJSONBytes {
		return agentPluginManifest{}, fmt.Errorf("Agent Plugin manifest exceeds the 1 MiB limit")
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return agentPluginManifest{}, fmt.Errorf("read Agent Plugin manifest: %w", err)
	}
	if err := validateUniqueJSON(contents); err != nil {
		return agentPluginManifest{}, fmt.Errorf("invalid plugin.json: %w", err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(contents, &fields); err != nil || fields == nil {
		return agentPluginManifest{}, fmt.Errorf("invalid plugin.json: expected a JSON object")
	}
	return validateAgentPluginManifest(fields)
}

// validateAgentPluginManifest checks standard identity and typed metadata fields.
func validateAgentPluginManifest(fields map[string]json.RawMessage) (agentPluginManifest, error) {
	var schema string
	if err := json.Unmarshal(fields["$schema"], &schema); err != nil || schema != agentPluginSchema {
		return agentPluginManifest{}, fmt.Errorf("plugin.json must declare $schema %s", agentPluginSchema)
	}
	var name string
	if err := json.Unmarshal(fields["name"], &name); err != nil || !validAgentPluginName(name) {
		return agentPluginManifest{}, fmt.Errorf("plugin.json name must use 1-64 lowercase letters, digits, hyphens or periods, without consecutive hyphens or periods")
	}
	if err := validateAgentPluginMetadata(fields); err != nil {
		return agentPluginManifest{}, err
	}
	return agentPluginManifest{Name: name}, nil
}

// validAgentPluginName mirrors the portable identity grammar without path separators.
func validAgentPluginName(name string) bool {
	return len(name) <= 64 && agentPluginName.MatchString(name) &&
		!strings.Contains(name, "--") && !strings.Contains(name, "..")
}

// validateAgentPluginMetadata enforces the standard's known field types.
func validateAgentPluginMetadata(fields map[string]json.RawMessage) error {
	for _, name := range []string{"version", "description", "homepage", "repository", "license"} {
		if value, found := fields[name]; found && !isJSONString(value) {
			return fmt.Errorf("plugin.json %s must be a string", name)
		}
	}
	if value, found := fields["keywords"]; found && !isJSONStringArray(value) {
		return fmt.Errorf("plugin.json keywords must be a list of strings")
	}
	if value, found := fields["author"]; found {
		if err := validateAgentPluginAuthor(value); err != nil {
			return err
		}
	}
	if value, found := fields["extensions"]; found && !isJSONObject(value) {
		return fmt.Errorf("plugin.json extensions must be a JSON object")
	}
	return nil
}

func isJSONString(value json.RawMessage) bool {
	var result string
	return json.Unmarshal(value, &result) == nil
}

func isJSONStringArray(value json.RawMessage) bool {
	var result []string
	return json.Unmarshal(value, &result) == nil
}

func isJSONObject(value json.RawMessage) bool {
	var result map[string]json.RawMessage
	return json.Unmarshal(value, &result) == nil && result != nil
}

// validateAgentPluginAuthor rejects ambiguous author metadata shapes.
func validateAgentPluginAuthor(value json.RawMessage) error {
	var author map[string]json.RawMessage
	if json.Unmarshal(value, &author) != nil || author == nil {
		return fmt.Errorf("plugin.json author must be an object containing only name, email and url")
	}
	for name, item := range author {
		if name != "name" && name != "email" && name != "url" {
			return fmt.Errorf("plugin.json author must contain only name, email and url")
		}
		if !isJSONString(item) {
			return fmt.Errorf("plugin.json author values must be strings")
		}
	}
	return nil
}

// validateUniqueJSON rejects duplicate fields at every object depth.
func validateUniqueJSON(contents []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(contents))
	decoder.UseNumber()
	first, err := decoder.Token()
	if err != nil {
		return err
	}
	if first != json.Delim('{') {
		return fmt.Errorf("expected a JSON object")
	}
	if err := scanJSONObject(decoder); err != nil {
		return err
	}
	if _, err := decoder.Token(); err != io.EOF {
		if err == nil {
			return fmt.Errorf("unexpected data after the JSON object")
		}
		return err
	}
	return nil
}

// scanJSONObject consumes one opened object while tracking field identity.
func scanJSONObject(decoder *json.Decoder) error {
	seen := make(map[string]struct{})
	for decoder.More() {
		field, err := decoder.Token()
		if err != nil {
			return err
		}
		name, ok := field.(string)
		if !ok {
			return fmt.Errorf("object field name must be a string")
		}
		if _, duplicate := seen[name]; duplicate {
			return fmt.Errorf("JSON contains duplicate field %q", name)
		}
		seen[name] = struct{}{}
		if err := scanJSONValue(decoder); err != nil {
			return err
		}
	}
	_, err := decoder.Token()
	return err
}

// scanJSONValue consumes one scalar or recursively opened collection.
func scanJSONValue(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	delimiter, ok := token.(json.Delim)
	if !ok {
		return nil
	}
	if delimiter == '{' {
		return scanJSONObject(decoder)
	}
	if delimiter != '[' {
		return fmt.Errorf("unexpected JSON delimiter")
	}
	for decoder.More() {
		if err := scanJSONValue(decoder); err != nil {
			return err
		}
	}
	_, err = decoder.Token()
	return err
}

// validatedAgentProject resolves an agent root and requires its stable marker.
func validatedAgentProject(project string) (string, error) {
	absolute, err := filepath.Abs(project)
	if err != nil {
		return "", fmt.Errorf("resolve Harnest agent root: %w", err)
	}
	root, err := filepath.EvalSymlinks(absolute)
	if err != nil {
		return "", fmt.Errorf("inspect Harnest agent root: %w", err)
	}
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return "", fmt.Errorf("Harnest agent root must be a directory: %s", absolute)
	}
	config, err := os.Lstat(filepath.Join(root, "config.yaml"))
	if err != nil || !config.Mode().IsRegular() {
		return "", fmt.Errorf("Harnest agent root must contain a regular config.yaml: %s", root)
	}
	return root, nil
}

// validateAgentPluginsRoot prevents an existing link from redirecting installation.
func validateAgentPluginsRoot(root string) error {
	return validateLocalPackageRoot(root, "plugins")
}

// pathContains reports whether candidate is root itself or nested beneath it.
func pathContains(root, candidate string) bool {
	relative, err := filepath.Rel(root, candidate)
	return err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator))
}

// installAgentPluginTree stages a complete copy and swaps it into place atomically.
func installAgentPluginTree(source, destination string, force bool) error {
	return installLocalPackageTree(source, destination, force, "Agent Plugin", "plugins")
}

// validateAgentPluginDestination refuses silent replacement and all linked destinations.
func validateAgentPluginDestination(destination string, force bool) error {
	return validateLocalPackageDestination(destination, force, "Agent Plugin")
}

// replaceAgentPluginDestination preserves the old package if the final rename fails.
func replaceAgentPluginDestination(staged, destination, stagingRoot string, force bool) error {
	return replaceLocalPackageDestination(
		staged, destination, stagingRoot, force, "Agent Plugin",
	)
}

// copyAgentPluginTree copies only the file types accepted during source validation.
func copyAgentPluginTree(source, destination string) error {
	return copyLocalPackageTree(source, destination, "Agent Plugin")
}

// copyAgentPluginFile creates a new regular file without following a target path.
func copyAgentPluginFile(source, destination string, mode fs.FileMode) error {
	return copyLocalPackageFile(source, destination, mode, "Agent Plugin")
}
