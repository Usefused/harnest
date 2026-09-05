package main

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

// validatedLocalPackageSource resolves a package root without following a linked root.
func validatedLocalPackageSource(source, label string) (string, error) {
	absolute, err := filepath.Abs(source)
	if err != nil {
		return "", fmt.Errorf("resolve %s source: %w", label, err)
	}
	info, err := os.Lstat(absolute)
	if err != nil {
		return "", fmt.Errorf("inspect %s source: %w", label, err)
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf("%s source cannot be a symlink: %s", label, absolute)
	}
	if !info.IsDir() {
		return "", fmt.Errorf("%s source must be a directory: %s", label, absolute)
	}
	if err := rejectLocalPackageTreeLinks(absolute, label); err != nil {
		return "", err
	}
	return absolute, nil
}

// rejectLocalPackageTreeLinks permits only ordinary directories and regular files.
func rejectLocalPackageTreeLinks(root, label string) error {
	return filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return fmt.Errorf("inspect %s source %s: %w", label, path, walkErr)
		}
		info, err := entry.Info()
		if err != nil {
			return fmt.Errorf("inspect %s resource %s: %w", label, path, err)
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("%s resources cannot be symlinks: %s", label, path)
		}
		if !info.IsDir() && !info.Mode().IsRegular() {
			return fmt.Errorf("%s resources must be regular files or directories: %s", label, path)
		}
		return nil
	})
}

// validateLocalPackageRoot prevents an existing link from redirecting installation.
func validateLocalPackageRoot(root, directoryLabel string) error {
	info, err := os.Lstat(root)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("inspect target %s directory: %w", directoryLabel, err)
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("target %s directory cannot be a symlink: %s", directoryLabel, root)
	}
	if !info.IsDir() {
		return fmt.Errorf("target %s path must be a directory: %s", directoryLabel, root)
	}
	return nil
}

// installLocalPackageTree stages a complete copy and swaps it into place atomically.
func installLocalPackageTree(
	source, destination string, force bool, packageLabel, directoryLabel string,
) error {
	if err := validateLocalPackageDestination(destination, force, packageLabel); err != nil {
		return err
	}
	parent := filepath.Dir(destination)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return fmt.Errorf("create target %s directory: %w", directoryLabel, err)
	}
	stagingRoot, err := os.MkdirTemp(parent, ".harnest-package-install-")
	if err != nil {
		return fmt.Errorf("create %s staging directory: %w", packageLabel, err)
	}
	defer os.RemoveAll(stagingRoot)
	staged := filepath.Join(stagingRoot, filepath.Base(destination))
	if err := copyLocalPackageTree(source, staged, packageLabel); err != nil {
		return err
	}
	// Repeat the check after staging so a concurrent local change is not silently replaced.
	if err := validateLocalPackageDestination(destination, force, packageLabel); err != nil {
		return err
	}
	return replaceLocalPackageDestination(staged, destination, stagingRoot, force, packageLabel)
}

// validateLocalPackageDestination refuses silent replacement and linked destinations.
func validateLocalPackageDestination(destination string, force bool, label string) error {
	info, err := os.Lstat(destination)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("inspect %s destination: %w", label, err)
	}
	if !force {
		return fmt.Errorf("%s already exists at %s; pass --force to replace it", label, destination)
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("existing %s destination must be a directory: %s", label, destination)
	}
	return nil
}

// replaceLocalPackageDestination restores the previous package if the final rename fails.
func replaceLocalPackageDestination(
	staged, destination, stagingRoot string, force bool, label string,
) error {
	backup := filepath.Join(stagingRoot, "previous")
	hadPrevious := false
	if force {
		if _, err := os.Lstat(destination); err == nil {
			hadPrevious = true
			if err := os.Rename(destination, backup); err != nil {
				return fmt.Errorf("stage existing %s for replacement: %w", label, err)
			}
		}
	}
	if err := os.Rename(staged, destination); err != nil {
		if hadPrevious {
			_ = os.Rename(backup, destination)
		}
		return fmt.Errorf("install %s: %w", label, err)
	}
	return nil
}

// copyLocalPackageTree preserves accepted file modes within the staged directory.
func copyLocalPackageTree(source, destination, label string) error {
	return filepath.WalkDir(source, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		target := filepath.Join(destination, relative)
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if info.IsDir() {
			return os.MkdirAll(target, info.Mode().Perm())
		}
		return copyLocalPackageFile(path, target, info.Mode().Perm(), label)
	})
}

// copyLocalPackageFile creates a new regular file without following a target path.
func copyLocalPackageFile(source, destination string, mode fs.FileMode, label string) error {
	contents, err := os.ReadFile(source)
	if err != nil {
		return fmt.Errorf("read %s resource %s: %w", label, source, err)
	}
	if err := os.WriteFile(destination, contents, mode); err != nil {
		return fmt.Errorf("write staged %s resource %s: %w", label, destination, err)
	}
	return nil
}
