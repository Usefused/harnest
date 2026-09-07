package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/engine"
)

const serveArtifactCacheVersion = "serve-artifact-v1"

var serveArtifactFingerprintPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

type serveArtifactCache struct {
	root string
	lock string
}

// prepareCachedServeArtifact reuses only a fully validated immutable generation.
func (a *application) prepareCachedServeArtifact(
	command *cobra.Command,
	bundle engine.Bundle,
	python pythonSelection,
) (string, bool, error) {
	cache, err := inspectServeArtifactCache(bundle.Directory)
	if err != nil {
		return "", false, err
	}
	fingerprint := serveArtifactFingerprint(bundle, python, a.version)
	if artifact, found := a.cachedServeArtifact(cache, bundle, fingerprint); found {
		pruneServeArtifactsAsync(cache.root, fingerprint)
		return artifact, true, nil
	}
	unlock, err := lockServeArtifactCache(cache.lock)
	if err != nil {
		return "", false, err
	}
	defer unlock()
	if artifact, found := a.cachedServeArtifact(cache, bundle, fingerprint); found {
		pruneServeArtifactsAsync(cache.root, fingerprint)
		return artifact, true, nil
	}
	artifact := filepath.Join(cache.root, fingerprint)
	if err := a.compileBundle(
		command, python, bundle, artifact, command.InOrStdin(),
	); err != nil {
		return "", false, err
	}
	if _, err := a.system.loadCompiledArtifact(artifact, bundle); err != nil {
		return "", false, fmt.Errorf("validate cached compiled artifact: %w", err)
	}
	// Publication is complete before removal starts, so a failed compilation
	// always leaves the previous generation available for another invocation.
	pruneServeArtifactsAsync(cache.root, fingerprint)
	return artifact, false, nil
}

// inspectServeArtifactCache creates only regular project-owned cache directories.
func inspectServeArtifactCache(project string) (serveArtifactCache, error) {
	root := filepath.Join(project, ".harnest", "artifacts")
	if err := ensureRegularArtifactDirectory(root); err != nil {
		return serveArtifactCache{}, err
	}
	return serveArtifactCache{
		root: root,
		lock: filepath.Join(project, ".harnest", "artifact.lock"),
	}, nil
}

// ensureRegularArtifactDirectory rejects links before cache writes or deletion.
func ensureRegularArtifactDirectory(path string) error {
	return ensureRegularPrivateDirectory(path, "compiled artifact cache")
}

// ensureRegularPrivateDirectory rejects links at compiler-owned storage roots.
func ensureRegularPrivateDirectory(path, label string) error {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		if err := os.MkdirAll(path, 0o755); err != nil {
			return fmt.Errorf("create %s %s: %w", label, path, err)
		}
		return nil
	}
	if err != nil {
		return fmt.Errorf("inspect %s %s: %w", label, path, err)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return fmt.Errorf("%s must be a regular directory: %s", label, path)
	}
	return nil
}

// serveArtifactFingerprint binds source and the exact managed compiler identity.
func serveArtifactFingerprint(
	bundle engine.Bundle, python pythonSelection, version string,
) string {
	digest := sha256.New()
	for _, value := range []string{
		serveArtifactCacheVersion,
		bundle.Digest,
		version,
		python.Executable,
	} {
		digest.Write([]byte(value))
		digest.Write([]byte{0})
	}
	return hex.EncodeToString(digest.Sum(nil))
}

// cachedServeArtifact rejects stale, corrupt, or source-mismatched generations.
func (a *application) cachedServeArtifact(
	cache serveArtifactCache, bundle engine.Bundle, fingerprint string,
) (string, bool) {
	artifact := filepath.Join(cache.root, fingerprint)
	if _, err := a.system.loadCompiledArtifact(artifact, bundle); err != nil {
		return "", false
	}
	return artifact, true
}

// lockServeArtifactCache prevents replicas from compiling the same generation twice.
func lockServeArtifactCache(path string) (func(), error) {
	if err := os.Mkdir(path, 0o755); err != nil {
		if os.IsExist(err) {
			return nil, fmt.Errorf("another compiled artifact update is in progress: %s", path)
		}
		return nil, fmt.Errorf("lock compiled artifact cache: %w", err)
	}
	return func() { _ = os.Remove(path) }, nil
}

// pruneServeArtifactsAsync removes superseded immutable generations off startup's path.
func pruneServeArtifactsAsync(root, current string) {
	stale := staleServeArtifacts(root, current)
	if len(stale) == 0 {
		return
	}
	go func() {
		for _, path := range stale {
			_ = os.RemoveAll(path)
		}
	}()
}

// staleServeArtifacts selects only cache-owned fingerprint directories for removal.
func staleServeArtifacts(root, current string) []string {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil
	}
	stale := make([]string, 0, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if name == current || !serveArtifactFingerprintPattern.MatchString(name) {
			continue
		}
		info, err := entry.Info()
		if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			continue
		}
		stale = append(stale, filepath.Join(root, name))
	}
	return stale
}

// managedServeArtifactCacheAvailable avoids stale caches for editable compilers.
func managedServeArtifactCacheAvailable(python pythonSelection) bool {
	return strings.TrimSpace(python.Source) == "agent environment"
}
