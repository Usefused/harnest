// Stamps, detects, and materializes standalone branded Harnest CLI binaries with appended pack payloads.

package packbinary

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"harnest.dev/harnest/internal/agentpack"
)

const (
	completeMarkerFile = ".complete"
	maxManifestBytes   = 4 << 20
	maxExtractBytes    = 512 << 20
)

// IsWindowsPE reports whether raw executable bytes begin with a Windows PE/MZ header.
func IsWindowsPE(data []byte) bool {
	return len(data) >= 2 && data[0] == 'M' && data[1] == 'Z'
}

// HasEmbeddedPackBytes reports whether raw executable bytes already carry a stamped pack payload.
func HasEmbeddedPackBytes(data []byte) bool {
	reader, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return false
	}
	for _, entry := range reader.File {
		if entry.Name == ManifestFile {
			return true
		}
	}
	return false
}

// WriteBinary appends a validated staged pack payload onto a base Harnest executable.
func WriteBinary(output string, baseBinary []byte, stagedDir string) (Manifest, error) {
	var manifest Manifest
	if HasEmbeddedPackBytes(baseBinary) {
		return manifest, fmt.Errorf("base binary already contains an embedded pack; use a standard harnest binary")
	}
	manifest, err := readStagedManifest(stagedDir)
	if err != nil {
		return manifest, err
	}
	if err := os.MkdirAll(filepath.Dir(output), 0o755); err != nil {
		return manifest, fmt.Errorf("create pack binary output directory: %w", err)
	}
	tempPath, err := writeStampedTempFile(filepath.Dir(output), baseBinary, stagedDir)
	if err != nil {
		return manifest, err
	}
	if err := os.Rename(tempPath, output); err != nil {
		_ = os.Remove(tempPath)
		return manifest, fmt.Errorf("publish pack binary: %w", err)
	}
	return manifest, nil
}

// readStagedManifest loads and validates harnest-pack-binary.json before stamping.
func readStagedManifest(stagedDir string) (Manifest, error) {
	var manifest Manifest
	data, err := os.ReadFile(filepath.Join(stagedDir, ManifestFile))
	if err != nil {
		return manifest, fmt.Errorf("read staged pack binary manifest: %w", err)
	}
	if err := json.Unmarshal(data, &manifest); err != nil {
		return manifest, fmt.Errorf("decode staged pack binary manifest: %w", err)
	}
	if err := manifest.Validate(); err != nil {
		return manifest, err
	}
	return manifest, nil
}

// writeStampedTempFile writes base bytes, appends the payload zip, extends Mach-O, and sets mode 0755.
func writeStampedTempFile(dir string, baseBinary []byte, stagedDir string) (string, error) {
	file, err := os.CreateTemp(dir, ".harnest-pack-bin-*")
	if err != nil {
		return "", fmt.Errorf("create temporary pack binary: %w", err)
	}
	tempPath := file.Name()
	cleanup := true
	defer func() {
		if cleanup {
			_ = os.Remove(tempPath)
		}
	}()
	if _, err := file.Write(baseBinary); err != nil {
		_ = file.Close()
		return "", fmt.Errorf("write base executable: %w", err)
	}
	if err := appendStagedZip(file, stagedDir); err != nil {
		_ = file.Close()
		return "", err
	}
	if err := agentpack.ExtendMachO(file); err != nil {
		_ = file.Close()
		return "", err
	}
	if err := file.Chmod(0o755); err != nil {
		_ = file.Close()
		return "", err
	}
	if err := file.Close(); err != nil {
		return "", err
	}
	cleanup = false
	return tempPath, nil
}

// appendStagedZip writes every regular file under stagedDir into an appended zip archive.
func appendStagedZip(file *os.File, stagedDir string) error {
	archive := zip.NewWriter(file)
	err := filepath.WalkDir(stagedDir, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil || entry.IsDir() {
			return walkErr
		}
		return addZipEntry(archive, stagedDir, path, entry)
	})
	if err != nil {
		_ = archive.Close()
		return err
	}
	return archive.Close()
}

// addZipEntry adds one validated regular file to the appended payload archive.
func addZipEntry(archive *zip.Writer, root, path string, entry os.DirEntry) error {
	info, err := entry.Info()
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("pack binary payload must contain regular files only: %s", path)
	}
	relative, err := filepath.Rel(root, path)
	if err != nil {
		return err
	}
	slash := filepath.ToSlash(relative)
	if !agentpack.SafePath(slash) {
		return fmt.Errorf("unsafe pack payload path %q", slash)
	}
	header := &zip.FileHeader{Name: slash, Method: zip.Deflate}
	header.SetMode(info.Mode().Perm())
	writer, err := archive.CreateHeader(header)
	if err != nil {
		return err
	}
	input, err := os.Open(path)
	if err != nil {
		return err
	}
	defer input.Close()
	_, err = io.Copy(writer, input)
	return err
}

// ReadManifest inspects an executable for an appended pack binary payload without extracting files.
func ReadManifest(binaryPath string) (Manifest, bool, error) {
	reader, closer, err := openPayloadZip(binaryPath)
	if err != nil || reader == nil {
		return Manifest{}, false, err
	}
	defer closer.Close()
	return decodeManifestFromZip(reader)
}

// openPayloadZip returns a zip reader when an executable carries a zip trailer, or (nil, nil, nil) otherwise.
func openPayloadZip(binaryPath string) (*zip.Reader, io.Closer, error) {
	file, err := os.Open(binaryPath)
	if err != nil {
		return nil, nil, err
	}
	end, err := agentpack.ExecutablePayloadEnd(file)
	if err != nil {
		_ = file.Close()
		return nil, nil, err
	}
	reader, err := zip.NewReader(file, end)
	if err != nil {
		// Ordinary Harnest binaries have no appended ZIP archive; treat as non-pack binary.
		_ = file.Close()
		return nil, nil, nil
	}
	return reader, file, nil
}

// decodeManifestFromZip parses harnest-pack-binary.json when present inside an appended archive.
func decodeManifestFromZip(reader *zip.Reader) (Manifest, bool, error) {
	var manifest Manifest
	entry, err := reader.Open(ManifestFile)
	if err != nil {
		// A ZIP trailer without harnest-pack-binary.json is not a standalone pack CLI.
		return manifest, false, nil
	}
	defer entry.Close()
	if err := json.NewDecoder(io.LimitReader(entry, maxManifestBytes)).Decode(&manifest); err != nil {
		return manifest, true, fmt.Errorf("decode embedded pack manifest: %w", err)
	}
	if err := manifest.Validate(); err != nil {
		return manifest, true, err
	}
	return manifest, true, nil
}

// Materialize detects an embedded pack on binaryPath and extracts it once into a content-addressed cache.
func Materialize(binaryPath, cacheRoot string) (*LoadedPack, error) {
	reader, closer, err := openPayloadZip(binaryPath)
	if err != nil || reader == nil {
		return nil, err
	}
	defer closer.Close()
	manifest, ok, err := decodeManifestFromZip(reader)
	if err != nil || !ok {
		return nil, err
	}
	base := filepath.Join(cacheRoot, "harnest", "pack-binaries")
	target := filepath.Join(base, fmt.Sprintf("%s-%s", manifest.Name, manifest.Digest[:16]))
	if cacheComplete(target, manifest.Digest) {
		return &LoadedPack{Manifest: manifest, Root: target}, nil
	}
	if err := extractToCache(reader, base, target, manifest.Digest); err != nil {
		return nil, err
	}
	return &LoadedPack{Manifest: manifest, Root: target}, nil
}

// cacheComplete verifies the target directory exists, is not a symlink, and matches the expected digest.
func cacheComplete(target, digest string) bool {
	info, err := os.Lstat(target)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return false
	}
	data, err := os.ReadFile(filepath.Join(target, completeMarkerFile))
	return err == nil && strings.TrimSpace(string(data)) == digest
}

// extractToCache unpacks all entries into a sibling staging folder and atomically publishes target.
func extractToCache(reader *zip.Reader, base, target, digest string) error {
	if err := os.MkdirAll(base, 0o755); err != nil {
		return fmt.Errorf("create pack binary cache directory: %w", err)
	}
	staging, err := os.MkdirTemp(base, ".extract-*")
	if err != nil {
		return fmt.Errorf("create pack binary staging directory: %w", err)
	}
	defer os.RemoveAll(staging)
	if err := extractZipEntries(reader, staging); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(staging, completeMarkerFile), []byte(digest+"\n"), 0o600); err != nil {
		return err
	}
	if err := os.Rename(staging, target); err != nil {
		// Another concurrent invocation may have published the identical digest first.
		if cacheComplete(target, digest) {
			return nil
		}
		return fmt.Errorf("publish materialized pack cache: %w", err)
	}
	return nil
}

// extractZipEntries unpacks regular files with strict containment and an aggregate size cap.
func extractZipEntries(reader *zip.Reader, destination string) error {
	var total int64
	for _, file := range reader.File {
		if file.FileInfo().IsDir() {
			continue
		}
		if !agentpack.SafePath(file.Name) || !file.Mode().IsRegular() {
			return fmt.Errorf("unsafe embedded pack path %q", file.Name)
		}
		written, err := extractSingleFile(file, filepath.Join(destination, filepath.FromSlash(file.Name)), maxExtractBytes-total)
		if err != nil {
			return err
		}
		total += written
	}
	return nil
}

// extractSingleFile writes one archive entry exclusively while enforcing the remaining byte budget.
func extractSingleFile(file *zip.File, target string, budget int64) (int64, error) {
	if budget < 0 {
		return 0, fmt.Errorf("embedded pack payload exceeds %d byte limit", maxExtractBytes)
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return 0, err
	}
	input, err := file.Open()
	if err != nil {
		return 0, err
	}
	defer input.Close()
	output, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return 0, err
	}
	written, copyErr := io.Copy(output, io.LimitReader(input, budget+1))
	if copyErr == nil && written > budget {
		copyErr = fmt.Errorf("embedded pack payload exceeds %d byte limit", maxExtractBytes)
	}
	modeErr := output.Chmod(file.Mode().Perm() & 0o777)
	closeErr := output.Close()
	for _, err := range []error{copyErr, modeErr, closeErr} {
		if err != nil {
			return written, err
		}
	}
	return written, nil
}
