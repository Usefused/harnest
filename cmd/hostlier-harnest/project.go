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
	Version   int    `yaml:"version"`
	BaseImage string `yaml:"baseImage"`
	Artifact  string `yaml:"artifact,omitempty"`
	// Bindings are explicitly consumed by authored lifecycle or sandbox factories.
	RequiredBindings []string `yaml:"requiredBindings,omitempty"`
}

var environmentName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// readSettings selects this package's inline settings, falling back to the original split-file layout.
func readSettings(root string, project map[string]any) (projectSettings, error) {
	if raw, present := project["packages"]; present {
		packages, ok := raw.(map[string]any)
		if !ok {
			return projectSettings{}, errors.New(".hostlier packages must be a mapping")
		}
		if value, present := packages["harnest"]; present {
			fields, ok := value.(map[string]any)
			if !ok {
				return projectSettings{}, errors.New(".hostlier packages.harnest must be a mapping")
			}
			// Enrollment alone creates an empty map and must preserve previously configured agents.
			if len(fields) > 0 {
				return inlineSettings(fields)
			}
		}
	}
	file, err := openProjectFile(root, "harnest-hostlier.yaml")
	if err != nil {
		return projectSettings{}, errors.New("set packages.harnest.baseImage in .hostlier to a digest-pinned Linux base image")
	}
	defer file.Close()
	return decodeSettings(file, 0)
}

// inlineSettings inherits the enclosing deployment format unless an explicit settings version is supplied.
func inlineSettings(fields map[string]any) (projectSettings, error) {
	data, err := yaml.Marshal(fields)
	if err != nil {
		return projectSettings{}, errors.New("invalid .hostlier packages.harnest")
	}
	return decodeSettings(bytes.NewReader(data), 1)
}

// decodeSettings keeps strict field and size checks identical across both configuration layouts.
func decodeSettings(reader io.Reader, defaultVersion int) (projectSettings, error) {
	s := projectSettings{Version: defaultVersion}
	data, err := io.ReadAll(io.LimitReader(reader, 65537))
	if err != nil || len(data) > 65536 {
		return s, errors.New("Harnest package settings exceed 64 KiB")
	}
	decoder := yaml.NewDecoder(bytes.NewReader(data))
	decoder.KnownFields(true)
	if decoder.Decode(&s) != nil || decoder.Decode(new(any)) != io.EOF {
		return s, errors.New("invalid Harnest package settings")
	}
	if s.Version != 1 || !pinnedImage.MatchString(s.BaseImage) {
		return s, errors.New("Harnest package settings require a supported version and a digest-pinned baseImage")
	}
	for _, name := range s.RequiredBindings {
		if !environmentName.MatchString(name) {
			return s, errors.New("invalid required environment binding name")
		}
	}
	return s, nil
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
