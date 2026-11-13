// Reads and validates packages.harnest settings from the Hostlier project.
package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"

	"gopkg.in/yaml.v3"
)

// projectSettings contains only Harnest packaging inputs. Hostlier owns placement, bindings and secrets.
type projectSettings struct {
	Name      string `yaml:"name,omitempty"`
	Version   int    `yaml:"version"`
	BaseImage string `yaml:"baseImage"`
	Artifact  string `yaml:"artifact,omitempty"`
	// Bindings are explicitly consumed by authored lifecycle or sandbox factories.
	RequiredBindings []string `yaml:"requiredBindings,omitempty"`
}

var agentName = regexp.MustCompile(`^[a-z][a-z0-9-]{0,62}$`)
var environmentName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// settingsSchema describes the same inline settings accepted by decodeSettings.
// Initialization may precede selection of the required packaging base image.
func settingsSchema() map[string]any {
	return map[string]any{
		"type": "object", "additionalProperties": false,
		"properties": map[string]any{
			"name":             map[string]any{"type": "string", "pattern": agentName.String(), "description": "Agent name for initialization. Omit to use --name or the project directory name."},
			"baseImage":        map[string]any{"type": "string", "pattern": pinnedImage.String(), "description": "Digest-pinned Linux image or multi-platform index. Plan selects the manifest for the target architecture."},
			"artifact":         map[string]any{"type": "string", "minLength": 1, "description": "Project-relative compiled agent executable. Omit to compile the source during packaging."},
			"requiredBindings": map[string]any{"type": "array", "items": map[string]any{"type": "string", "pattern": environmentName.String()}, "description": "Environment names the agent needs from the deployment or another package."},
			"version":          map[string]any{"type": "integer", "const": 1, "description": "Settings format version. Omit to use version 1."},
		},
		"if": map[string]any{"anyOf": []map[string]any{{"required": []string{"artifact"}}, {"required": []string{"version"}}, {"required": []string{"requiredBindings"}}}}, "then": map[string]any{"required": []string{"baseImage"}},
	}
}

// inlineSettings inherits the enclosing deployment format unless an explicit settings version is supplied.
func inlineSettings(fields map[string]any) (projectSettings, error) {
	data, err := yaml.Marshal(fields)
	if err != nil {
		return projectSettings{}, errors.New("invalid .hostlier packages.harnest")
	}
	return decodeSettings(bytes.NewReader(data))
}

// decodeSettings checks the inline config transmitted in the hook request.
func decodeSettings(reader io.Reader) (projectSettings, error) {
	s := projectSettings{Version: 1}
	data, err := io.ReadAll(io.LimitReader(reader, 65537))
	if err != nil || len(data) > 65536 {
		return s, errors.New("Harnest package settings exceed 64 KiB")
	}
	decoder := yaml.NewDecoder(bytes.NewReader(data))
	decoder.KnownFields(true)
	if decoder.Decode(&s) != nil || decoder.Decode(new(any)) != io.EOF {
		return s, errors.New("invalid Harnest package settings")
	}
	return s, s.validate()
}

// validate rejects invalid package settings before planning or building.
func (s projectSettings) validate() error {
	if s.Name != "" && !agentName.MatchString(s.Name) {
		return errors.New("invalid packages.harnest.name")
	}
	if s.Version != 1 || !pinnedImage.MatchString(s.BaseImage) {
		return errors.New("set packages.harnest.baseImage to a digest-pinned Linux image and use settings version 1")
	}
	for _, name := range s.RequiredBindings {
		if !environmentName.MatchString(name) {
			return errors.New("invalid required environment binding name")
		}
	}
	return nil
}

// openProjectFile confines package inputs to regular files under the application root, including symlink resolution.
func openProjectFile(root, name string) (*os.File, error) {
	if !filepath.IsLocal(name) {
		return nil, errors.New("package input must be project-relative")
	}
	dir, err := os.OpenRoot(root)
	if err != nil {
		return nil, err
	}
	defer dir.Close()
	info, err := dir.Stat(name)
	if err != nil || !info.Mode().IsRegular() {
		return nil, fmt.Errorf("missing or invalid Harnest project input %s", name)
	}
	return dir.Open(name)
}
