package agentpack

import (
	"fmt"
	"os"
	"path/filepath"
)

// Materialize builds a verified runtime tree and shares immutable files across packs.
func Materialize(pack, cache string, m Manifest) (string, error) {
	if err := m.Validate(); err != nil {
		return "", err
	}
	if err := m.CheckHost(); err != nil {
		return "", err
	}
	if err := os.MkdirAll(cache, 0700); err != nil {
		return "", err
	}
	destination := filepath.Join(cache, "runtimes", m.Digest)
	if _, err := os.Lstat(destination); err == nil {
		return destination, verifyTree(destination, m)
	}
	return publishRuntimeTree(pack, cache, destination, m)
}

// publishRuntimeTree makes concurrent launches converge on one complete immutable tree.
func publishRuntimeTree(pack, cache, destination string, m Manifest) (string, error) {
	if err := os.MkdirAll(filepath.Dir(destination), 0700); err != nil {
		return "", err
	}
	staging, err := os.MkdirTemp(filepath.Dir(destination), ".runtime-*")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(staging)
	if err = materializeFiles(pack, cache, staging, m.Files); err != nil {
		return "", err
	}
	if err = WriteManifest(staging, m); err != nil {
		return "", err
	}
	if err = os.Rename(staging, destination); err != nil {
		// Concurrent launchers may publish the same immutable identity first.
		if verifyErr := verifyTree(destination, m); verifyErr != nil {
			return "", err
		}
	}
	return destination, nil
}

// materializeFiles imports each object once into the shared deployment cache.
func materializeFiles(pack, cache, staging string, files []File) error {
	imported := make(map[string]bool)
	for _, f := range files {
		target := filepath.Join(staging, filepath.FromSlash(f.Path))
		if err := os.MkdirAll(filepath.Dir(target), 0700); err != nil {
			return err
		}
		if f.Link != "" {
			if err := os.Symlink(filepath.FromSlash(f.Link), target); err != nil {
				return err
			}
			continue
		}
		object := filepath.Join(cache, "objects", f.Object)
		if !imported[f.Object] {
			if err := LinkObject(filepath.Join(pack, "objects", f.Object), object, f); err != nil {
				return err
			}
			imported[f.Object] = true
		}
		if err := publishObject(object, target, f); err != nil {
			return err
		}
	}
	return nil
}

// verifyTree rejects changed content or links before reusing an existing runtime.
func verifyTree(root string, m Manifest) error {
	existing, err := ReadManifest(root)
	if err != nil {
		return err
	}
	if existing.Digest != m.Digest {
		return fmt.Errorf("cached runtime identity mismatch")
	}
	for _, f := range m.Files {
		target, err := containedFile(root, f.Path)
		if err != nil {
			return err
		}
		if f.Link != "" {
			link, err := os.Readlink(target)
			if err != nil || filepath.ToSlash(link) != f.Link {
				return fmt.Errorf("cached runtime link mismatch: %s", f.Path)
			}
			continue
		}
		if err := VerifyObject(target, f); err != nil {
			return err
		}
	}
	return rejectExtraFiles(root, m)
}

// rejectExtraFiles prevents injected modules from altering an otherwise hash-valid cache.
func rejectExtraFiles(root string, m Manifest) error {
	expected := map[string]bool{ManifestFile: true}
	for _, f := range m.Files {
		expected[f.Path] = true
	}
	return filepath.WalkDir(root, func(filename string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return nil
		}
		relative, err := filepath.Rel(root, filename)
		if err != nil {
			return err
		}
		if !expected[filepath.ToSlash(relative)] {
			return fmt.Errorf("unmanifested runtime file: %s", relative)
		}
		return nil
	})
}

// containedFile rejects substituted symlink parents even in an existing cache.
func containedFile(root, relative string) (string, error) {
	if !SafePath(relative) {
		return "", fmt.Errorf("unsafe payload path %q", relative)
	}
	target := filepath.Join(root, filepath.FromSlash(relative))
	for parent := filepath.Dir(target); ; parent = filepath.Dir(parent) {
		info, err := os.Lstat(parent)
		if err != nil {
			return "", err
		}
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return "", fmt.Errorf("unsafe payload directory %s", parent)
		}
		if parent == root {
			break
		}
	}
	return target, nil
}
