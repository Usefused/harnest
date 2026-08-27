package engine

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

var ignoredDirectories = map[string]bool{
	".adk": true, ".git": true, ".harnest": true, ".mypy_cache": true, ".pytest_cache": true,
	".ruff_cache": true, ".venv": true, "__pycache__": true, "venv": true,
}

func digestDirectory(root string) (string, error) {
	var files []string
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() && path != root && ignoredDirectories[entry.Name()] {
			return filepath.SkipDir
		}
		if entry.Type().IsRegular() && !ignoredBundleFile(entry.Name()) {
			files = append(files, path)
		}
		return nil
	})
	if err != nil {
		return "", fmt.Errorf("walk agent bundle %s: %w", root, err)
	}
	sort.Strings(files)
	hash := sha256.New()
	for _, path := range files {
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return "", fmt.Errorf("make %s relative to %s: %w", path, root, err)
		}
		relative = filepath.ToSlash(relative)
		file, err := os.Open(path)
		if err != nil {
			return "", fmt.Errorf("hash %s: %w", path, err)
		}
		info, err := file.Stat()
		if err != nil {
			_ = file.Close()
			return "", fmt.Errorf("stat %s: %w", path, err)
		}
		if err := binary.Write(hash, binary.BigEndian, uint64(len(relative))); err != nil {
			_ = file.Close()
			return "", fmt.Errorf("hash path length for %s: %w", path, err)
		}
		_, _ = io.WriteString(hash, relative)
		if err := binary.Write(hash, binary.BigEndian, uint64(info.Size())); err != nil {
			_ = file.Close()
			return "", fmt.Errorf("hash file length for %s: %w", path, err)
		}
		_, copyErr := io.Copy(hash, file)
		closeErr := file.Close()
		if copyErr != nil {
			return "", fmt.Errorf("hash %s: %w", path, copyErr)
		}
		if closeErr != nil {
			return "", fmt.Errorf("close %s: %w", path, closeErr)
		}
	}
	return "sha256:" + hex.EncodeToString(hash.Sum(nil)), nil
}

func ignoredBundleFile(name string) bool {
	return name == ".env" || strings.HasPrefix(name, ".env.") ||
		strings.HasSuffix(name, ".pyc") || strings.HasSuffix(name, ".pyo")
}

var nowUTC = func() time.Time { return time.Now().UTC() }
