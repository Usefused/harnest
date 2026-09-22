// Package agentpack stores immutable portable runtimes and agent payloads.
package agentpack

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
)

const ManifestFile = "runtime-manifest.json"
const Format = "harnest.runtime/v1"

// File binds a relative path to one immutable object or a contained symbolic link.
type File struct {
	Path   string `json:"path"`
	Object string `json:"object,omitempty"`
	Link   string `json:"link,omitempty"`
	Size   int64  `json:"size,omitempty"`
}

// Manifest describes a platform-specific dependency closure shared by agents.
type Manifest struct {
	Format  string   `json:"format"`
	OS      string   `json:"os"`
	Arch    string   `json:"arch"`
	Python  string   `json:"python"`
	Version string   `json:"pythonVersion"`
	Harnest string   `json:"harnestVersion"`
	Inputs  []string `json:"inputs"`
	Files   []File   `json:"files"`
	Digest  string   `json:"digest"`
}

// Seal canonicalizes identities so discovery order cannot change a pack digest.
func (m *Manifest) Seal() {
	sort.Strings(m.Inputs)
	sort.Slice(m.Files, func(i, j int) bool { return m.Files[i].Path < m.Files[j].Path })
	m.Digest = m.Identity()
}

// Identity binds platform, requirements and the complete file inventory.
func (m Manifest) Identity() string {
	m.Digest = ""
	data, _ := json.Marshal(m)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// SafePath excludes traversal and platform-specific alternate path spellings.
func SafePath(value string) bool {
	return value != "." && value != "" && path.IsAbs(value) == false &&
		path.Clean(value) == value && value != ".." && !strings.HasPrefix(value, "../") &&
		!strings.ContainsAny(value, "\\:\x00") && (runtime.GOOS != "windows" || safeWindowsPath(value))
}

// Validate rejects ambiguous trees before any files or symlinks are materialized.
func (m Manifest) Validate() error {
	if m.Format != Format || m.Digest != m.Identity() || len(m.Files) == 0 {
		return fmt.Errorf("invalid runtime manifest identity or format")
	}
	if m.OS == "windows" {
		if err := validateWindowsTree(m.Files); err != nil {
			return err
		}
	}
	paths := make(map[string]File, len(m.Files))
	previous := ""
	for _, f := range m.Files {
		if err := validateFile(f, previous, paths); err != nil {
			return err
		}
		paths[f.Path] = f
		previous = f.Path
	}
	return m.validatePython(paths)
}

// validatePython requires a regular executable explicitly bound to the inventory.
func (m Manifest) validatePython(paths map[string]File) error {
	if !SafePath(m.Python) {
		return fmt.Errorf("invalid runtime Python path")
	}
	if f, ok := paths[m.Python]; !ok || f.Object == "" || !strings.HasSuffix(f.Object, "-555") {
		return fmt.Errorf("runtime Python must be a manifest-bound executable")
	}
	return nil
}

// validateFile prevents descendants beneath files or links, including link escapes.
func validateFile(f File, previous string, paths map[string]File) error {
	if !SafePath(f.Path) || f.Path <= previous || f.Size < 0 {
		return fmt.Errorf("invalid or unsorted runtime path %q", f.Path)
	}
	for parent := path.Dir(f.Path); parent != "."; parent = path.Dir(parent) {
		if _, exists := paths[parent]; exists {
			return fmt.Errorf("runtime path crosses file or link: %s", f.Path)
		}
	}
	if f.Link != "" {
		return validateLink(f)
	}

	_, err := objectMode(f.Object)
	return err
}

// validateLink keeps symbolic link targets inside the immutable payload tree.
func validateLink(f File) error {
	target := path.Clean(path.Join(path.Dir(f.Path), f.Link))
	if f.Object != "" || path.IsAbs(f.Link) || strings.ContainsAny(f.Link, "\\:\x00") || !SafePath(target) {
		return fmt.Errorf("unsafe runtime link %q", f.Path)
	}
	return nil
}

// CheckHost fails before execution when an attached pack targets another platform.
func (m Manifest) CheckHost() error {
	if err := SupportedHost(); err != nil {
		return err
	}
	if m.OS != runtime.GOOS || m.Arch != runtime.GOARCH {
		return fmt.Errorf("runtime targets %s/%s; host is %s/%s", m.OS, m.Arch, runtime.GOOS, runtime.GOARCH)
	}
	return nil
}

// ReadManifest verifies metadata without trusting a mutable completion marker.
func ReadManifest(root string) (Manifest, error) {
	var m Manifest
	info, err := os.Lstat(filepath.Join(root, ManifestFile))
	if err != nil {
		return m, err
	}
	if !info.Mode().IsRegular() || info.Size() > 64<<20 {
		return m, fmt.Errorf("invalid runtime manifest file")
	}
	data, err := os.ReadFile(filepath.Join(root, ManifestFile))
	if err != nil {
		return m, err
	}
	if err = json.Unmarshal(data, &m); err != nil {
		return m, err
	}
	return m, m.Validate()
}

// WriteManifest publishes metadata only after the complete object set exists.
func WriteManifest(root string, m Manifest) error {
	if err := m.Validate(); err != nil {
		return err
	}
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(root, ManifestFile), append(data, '\n'), 0600)
}

// SupportedHost enumerates platforms with portable runtime and launcher implementations.
func SupportedHost() error {
	if runtime.GOOS != "linux" && runtime.GOOS != "darwin" && runtime.GOOS != "windows" {
		return fmt.Errorf("native runtime packs support Linux, macOS and Windows; use directory compilation on %s", runtime.GOOS)
	}
	return nil
}
