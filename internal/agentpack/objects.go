package agentpack

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// objectMode includes executable permissions in identity to keep hardlinks immutable.
func objectMode(id string) (os.FileMode, error) {
	if len(id) != 68 {
		return 0, fmt.Errorf("invalid runtime object %q", id)
	}
	if _, err := hex.DecodeString(id[:64]); err != nil {
		return 0, fmt.Errorf("invalid runtime object hash")
	}
	switch id[64:] {
	case "-444":
		return 0444, nil
	case "-555":
		return 0555, nil
	default:
		return 0, fmt.Errorf("invalid runtime object mode")
	}
}

// VerifyObject hashes bytes rather than assuming a digest-shaped filename is trustworthy.
func VerifyObject(filename string, f File) error {
	mode, err := objectMode(f.Object)
	if err != nil {
		return err
	}
	info, err := os.Lstat(filename)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Size() != f.Size || info.Mode().Perm() != storedPermissions(mode) {
		return fmt.Errorf("runtime object size, type or permissions mismatch: %s", f.Path)
	}
	input, err := os.Open(filename)
	if err != nil {
		return err
	}
	defer input.Close()
	hash := sha256.New()
	if _, err = io.Copy(hash, input); err != nil {
		return err
	}
	if hex.EncodeToString(hash.Sum(nil)) != f.Object[:64] {
		return fmt.Errorf("runtime object checksum mismatch: %s", f.Path)
	}
	return nil
}

// StoreFile streams each dependency once and atomically publishes its content identity.
func StoreFile(source, store string) (File, error) {
	input, err := os.Open(source)
	if err != nil {
		return File{}, err
	}
	defer input.Close()
	info, err := input.Stat()
	if err != nil {
		return File{}, err
	}
	if !info.Mode().IsRegular() {
		return File{}, fmt.Errorf("cannot package non-regular file %s", source)
	}
	if err = os.MkdirAll(store, 0700); err != nil {
		return File{}, err
	}
	tmp, err := os.CreateTemp(store, ".object-*")
	if err != nil {
		return File{}, err
	}
	defer os.Remove(tmp.Name())
	defer tmp.Close()
	f, err := writeObject(tmp, input, sourcePermissions(source, info.Mode()))
	if err != nil {
		return File{}, err
	}
	if err = publishObject(tmp.Name(), filepath.Join(store, f.Object), f); err != nil {
		return File{}, err
	}
	return f, nil
}

// writeObject computes identity and final permissions before bytes become shared.
func writeObject(tmp *os.File, input io.Reader, permissions os.FileMode) (File, error) {
	hash := sha256.New()
	size, err := io.Copy(io.MultiWriter(tmp, hash), input)
	if err != nil {
		return File{}, err
	}
	mode := os.FileMode(0444)
	if permissions&0111 != 0 {
		mode = 0555
	}
	id := fmt.Sprintf("%s-%03o", hex.EncodeToString(hash.Sum(nil)), mode)
	f := File{Object: id, Size: size}
	if err = tmp.Chmod(storedPermissions(mode)); err != nil {
		return File{}, err
	}
	if err = tmp.Close(); err != nil {
		return File{}, err
	}
	return f, nil
}

// publishObject never overwrites a shared inode used by another live runtime.
func publishObject(source, destination string, f File) error {
	if err := os.Link(source, destination); err == nil {
		return nil
	}
	if _, err := os.Lstat(destination); err == nil {
		return VerifyObject(destination, f)
	}
	return copyExclusive(source, destination, f)
}

// copyExclusive preserves correctness on filesystems without hardlink support.
func copyExclusive(source, destination string, f File) error {
	mode, err := objectMode(f.Object)
	if err != nil {
		return err
	}
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	output, err := os.CreateTemp(filepath.Dir(destination), ".copy-*")
	if err != nil {
		return err
	}
	defer os.Remove(output.Name())
	defer output.Close()
	_, copyErr := io.Copy(output, input)
	modeErr := output.Chmod(storedPermissions(mode))
	closeErr := output.Close()
	for _, err := range []error{copyErr, modeErr, closeErr} {
		if err != nil {
			return err
		}
	}
	if err := VerifyObject(output.Name(), f); err != nil {
		return err
	}
	return publishCopiedObject(output.Name(), destination, f)
}

// publishCopiedObject uses a same-volume link for atomic no-replace publication after a cross-volume copy.
func publishCopiedObject(source, destination string, f File) error {
	if err := os.Link(source, destination); err == nil {
		return nil
	}
	if _, err := os.Lstat(destination); err == nil {
		return VerifyObject(destination, f)
	}
	// Filesystems without links still see complete verified bytes at the final name.
	return os.Rename(source, destination)
}

// LinkObject reuses existing bytes and rejects corruption before sharing them.
func LinkObject(source, destination string, f File) error {
	if err := VerifyObject(source, f); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(destination), 0700); err != nil {
		return err
	}
	return publishObject(source, destination, f)
}

// ScanTree preserves resources and relative links while excluding nonportable bytecode.
func ScanTree(root, prefix, store string) ([]File, error) {
	var files []File
	err := filepath.WalkDir(root, func(filename string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(root, filename)
		if err != nil {
			return err
		}
		if skipRuntimePath(relative, entry.IsDir()) || (prefix == "python" && entry.IsDir() && entry.Name() == "site-packages") {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if entry.IsDir() {
			return nil
		}
		f, err := scanFile(root, filename, store, entry)
		if err != nil {
			return err
		}
		f.Path = filepath.ToSlash(filepath.Join(prefix, relative))
		files = append(files, f)
		return nil
	})
	return files, err
}

// skipRuntimePath avoids machine-specific caches and executable installer metadata.
func skipRuntimePath(relative string, directory bool) bool {
	name := filepath.Base(relative)
	if directory {
		return name == "__pycache__"
	}
	return strings.HasSuffix(name, ".pyc") || strings.HasSuffix(name, ".pyo")
}

// scanFile translates absolute in-tree Python aliases to relocatable relative links.
func scanFile(root, filename, store string, entry os.DirEntry) (File, error) {
	if entry.Type()&os.ModeSymlink == 0 {
		return StoreFile(filename, store)
	}
	resolved, err := filepath.EvalSymlinks(filename)
	if err != nil {
		return File{}, err
	}
	relative, err := filepath.Rel(root, resolved)
	if err != nil || !SafePath(filepath.ToSlash(relative)) {
		return File{}, fmt.Errorf("runtime link escapes payload: %s", filename)
	}
	if runtime.GOOS == "windows" {
		// Flatten file aliases into shared objects; deployments need no symlink privilege.
		return StoreFile(resolved, store)
	}
	target, err := filepath.Rel(filepath.Dir(filename), resolved)
	return File{Link: filepath.ToSlash(target)}, err
}
