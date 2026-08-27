// Package runtimewheel owns the Python wheel embedded in release CLI binaries.
package runtimewheel

import (
	"archive/zip"
	"bytes"
	"embed"
	"fmt"
	"io"
	"io/fs"
	"path"
	"strings"
)

const assetsDirectory = "assets"

// The placeholder keeps ordinary source builds valid. GoReleaser adds exactly
// one wheel to this directory before compiling official release binaries.
//
//go:embed assets
var embeddedAssets embed.FS

// Artifact is the version-validated Python runtime carried by the Go CLI.
type Artifact struct {
	Name     string
	Contents []byte
}

// Embedded returns the release wheel only when its metadata matches the CLI.
func Embedded(expectedVersion string) (Artifact, error) {
	return artifactForVersion(embeddedAssets, expectedVersion)
}

func artifactForVersion(files fs.FS, expectedVersion string) (Artifact, error) {
	artifact, err := artifactFromFS(files)
	if err != nil {
		return Artifact{}, err
	}
	version, err := metadataVersion(artifact.Contents)
	if err != nil {
		return Artifact{}, fmt.Errorf("inspect embedded Harnest wheel: %w", err)
	}
	expected := strings.TrimPrefix(expectedVersion, "v")
	if version != expected {
		return Artifact{}, fmt.Errorf(
			"embedded Harnest wheel version %s does not match CLI version %s",
			version,
			expected,
		)
	}
	return artifact, nil
}

func artifactFromFS(files fs.FS) (Artifact, error) {
	entries, err := fs.ReadDir(files, assetsDirectory)
	if err != nil {
		return Artifact{}, fmt.Errorf("read embedded runtime assets: %w", err)
	}
	var wheelNames []string
	for _, entry := range entries {
		if !entry.IsDir() && strings.HasSuffix(entry.Name(), ".whl") {
			wheelNames = append(wheelNames, entry.Name())
		}
	}
	if len(wheelNames) != 1 {
		return Artifact{}, fmt.Errorf(
			"release binary must contain exactly one Harnest wheel; found %d",
			len(wheelNames),
		)
	}
	contents, err := fs.ReadFile(files, path.Join(assetsDirectory, wheelNames[0]))
	if err != nil {
		return Artifact{}, fmt.Errorf("read embedded Harnest wheel: %w", err)
	}
	return Artifact{Name: wheelNames[0], Contents: contents}, nil
}

func metadataVersion(contents []byte) (string, error) {
	reader, err := zip.NewReader(bytes.NewReader(contents), int64(len(contents)))
	if err != nil {
		return "", fmt.Errorf("open wheel: %w", err)
	}
	for _, file := range reader.File {
		if strings.HasSuffix(file.Name, ".dist-info/METADATA") {
			return versionFromMetadata(file)
		}
	}
	return "", fmt.Errorf("wheel has no distribution METADATA")
}

func versionFromMetadata(file *zip.File) (string, error) {
	stream, err := file.Open()
	if err != nil {
		return "", fmt.Errorf("open %s: %w", file.Name, err)
	}
	defer stream.Close()
	contents, err := io.ReadAll(stream)
	if err != nil {
		return "", fmt.Errorf("read %s: %w", file.Name, err)
	}
	for _, line := range strings.Split(string(contents), "\n") {
		if value, found := strings.CutPrefix(line, "Version: "); found {
			return strings.TrimSpace(value), nil
		}
	}
	return "", fmt.Errorf("%s has no Version field", file.Name)
}
