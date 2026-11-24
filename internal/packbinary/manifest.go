// Defines and validates the manifest embedded inside a standalone branded Harnest pack binary.

package packbinary

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strings"

	"harnest.dev/harnest/internal/agentpack"
)

const (
	// Format identifies the versioned appended-ZIP contract for standalone pack binaries.
	Format = "harnest.pack-binary/v1"
	// ManifestFile is the root JSON entry inside a pack binary's appended ZIP archive.
	ManifestFile = "harnest-pack-binary.json"
)

var (
	identifierPattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,62}$`)
	sha256Pattern     = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// CommandEntry describes one company subcommand registered in pack.py.
type CommandEntry struct {
	Name string `json:"name"`
	Help string `json:"help"`
}

// SkillEntry locates one pack-owned coding-IDE skill inside the materialized payload.
type SkillEntry struct {
	Name string `json:"name"`
	Root string `json:"root"`
}

// WheelEntry locates one bundled Python wheel enforced by the pack binary.
type WheelEntry struct {
	Name     string `json:"name"`
	Filename string `json:"filename"`
	SHA256   string `json:"sha256"`
}

// ExtensionEntry locates one pack-owned Harnest Extension auto-loaded without copying into agent repos.
type ExtensionEntry struct {
	Name         string `json:"name"`
	Path         string `json:"path"`
	HasPyproject bool   `json:"hasPyproject"`
}

// Manifest holds validated metadata for an embedded company pack payload.
type Manifest struct {
	Format            string           `json:"format"`
	Name              string           `json:"name"`
	Version           string           `json:"version"`
	Title             string           `json:"title"`
	Description       string           `json:"description"`
	Command           string           `json:"command"`
	HasProjectHooks   bool             `json:"hasProjectHooks"`
	Commands          []CommandEntry   `json:"commands"`
	Skills            []SkillEntry     `json:"skills"`
	Wheels            []WheelEntry     `json:"wheels"`
	Requirements      []string         `json:"requirements"`
	ProtectedPackages []string         `json:"protectedPackages"`
	Extensions        []ExtensionEntry `json:"extensions"`
	Digest            string           `json:"digest"`
}

// LoadedPack pairs a validated embedded pack manifest with its materialized cache directory.
type LoadedPack struct {
	Manifest Manifest
	Root     string
}

// Validate rejects malformed or unsafe pack binary metadata before materialization.
func (m Manifest) Validate() error {
	if m.Format != Format {
		return fmt.Errorf("unsupported pack binary format %q", m.Format)
	}
	if !identifierPattern.MatchString(m.Name) || m.Name == "harnest" {
		return fmt.Errorf("invalid embedded pack name %q", m.Name)
	}
	if !identifierPattern.MatchString(m.Command) || m.Command == "harnest" {
		return fmt.Errorf("invalid embedded pack command %q", m.Command)
	}
	if strings.TrimSpace(m.Version) == "" || !sha256Pattern.MatchString(m.Digest) {
		return fmt.Errorf("invalid embedded pack version or digest")
	}
	return m.validateEntries()
}

// validateEntries checks every declared skill, wheel, and extension path stays contained.
func (m Manifest) validateEntries() error {
	if err := validateSkillEntries(m.Skills); err != nil {
		return err
	}
	if err := validateWheelEntries(m.Wheels); err != nil {
		return err
	}
	return validateExtensionEntries(m.Extensions)
}

// validateSkillEntries ensures embedded skill names and archive paths are safe.
func validateSkillEntries(skills []SkillEntry) error {
	for _, skill := range skills {
		if !identifierPattern.MatchString(skill.Name) || !agentpack.SafePath(skill.Root) {
			return fmt.Errorf("invalid embedded skill entry %q", skill.Name)
		}
	}
	return nil
}

// validateWheelEntries ensures bundled wheel metadata carries valid names, paths, and digests.
func validateWheelEntries(wheels []WheelEntry) error {
	for _, wheel := range wheels {
		if strings.TrimSpace(wheel.Name) == "" || !agentpack.SafePath(wheel.Filename) || !sha256Pattern.MatchString(wheel.SHA256) {
			return fmt.Errorf("invalid embedded wheel entry %q", wheel.Name)
		}
	}
	return nil
}

// validateExtensionEntries ensures embedded extension paths stay inside the archive root.
func validateExtensionEntries(extensions []ExtensionEntry) error {
	for _, ext := range extensions {
		if strings.TrimSpace(ext.Name) == "" || !agentpack.SafePath(ext.Path) {
			return fmt.Errorf("invalid embedded extension entry %q", ext.Name)
		}
	}
	return nil
}

// PackDir returns the materialized pack folder passed to init, upgrade, studio, and pack run.
func (l *LoadedPack) PackDir() string {
	return filepath.Join(l.Root, "pack")
}

// WheelsDir returns the materialized directory containing bundled pack wheels.
func (l *LoadedPack) WheelsDir() string {
	return filepath.Join(l.Root, "wheels")
}

// ExtensionsDir returns the materialized directory containing pack-owned Harnest Extensions.
func (l *LoadedPack) ExtensionsDir() string {
	return filepath.Join(l.Root, "extensions")
}

// SkillDir resolves the materialized filesystem directory for one embedded coding-IDE skill.
func (l *LoadedPack) SkillDir(skill SkillEntry) string {
	return filepath.Join(l.Root, filepath.FromSlash(skill.Root))
}

// WheelPath resolves the materialized wheel file path for one embedded wheel entry.
func (l *LoadedPack) WheelPath(wheel WheelEntry) string {
	return filepath.Join(l.Root, filepath.FromSlash(wheel.Filename))
}

// ExtensionProjectFiles returns pyproject.toml paths for every embedded extension that declares one.
func (l *LoadedPack) ExtensionProjectFiles() []string {
	var projects []string
	for _, ext := range l.Manifest.Extensions {
		if ext.HasPyproject {
			projects = append(projects, filepath.Join(l.Root, filepath.FromSlash(ext.Path), "pyproject.toml"))
		}
	}
	return projects
}
