package engine

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

const (
	compiledManifestFilename     = "harnest-manifest.json"
	compiledServerConfigFilename = "server.yaml"
)

type Compiler interface {
	Compile(context.Context, Bundle) (CompiledArtifact, error)
}

// PythonCompiler invokes the Python authoring compiler without a shell. Each
// agent is compiled into an isolated child of OutputRoot.
type PythonCompiler struct {
	Python     string
	OutputRoot string
	Stderr     io.Writer
}

func (c PythonCompiler) Compile(ctx context.Context, bundle Bundle) (CompiledArtifact, error) {
	outputDirectory, err := c.prepare(ctx, bundle)
	if err != nil {
		return CompiledArtifact{}, err
	}
	command := exec.CommandContext(ctx, c.Python, "-m", "harnest.cli", "compile", bundle.Directory,
		"--output", outputDirectory,
		"--entrypoint", bundle.Config.Spec.Entrypoint,
		"--framework", bundle.Config.Spec.Framework.Name,
		"--mode", bundle.Config.Spec.Framework.EffectiveMode())
	command.Stderr = c.Stderr
	command.Env = compilerEnvironment(bundle.Config.Spec.Environment)
	var stdout bytes.Buffer
	command.Stdout = &stdout
	if err := command.Run(); err != nil {
		return CompiledArtifact{}, fmt.Errorf("run harnest compile for %s: %w", bundle.Config.Metadata.Name, err)
	}
	stdoutManifest, err := decodeCompiledManifest(bytes.NewReader(stdout.Bytes()))
	if err != nil {
		return CompiledArtifact{}, fmt.Errorf("decode compiler output for %s: %w", bundle.Config.Metadata.Name, err)
	}
	artifact, err := loadCompiledArtifact(outputDirectory, bundle)
	if err != nil {
		return CompiledArtifact{}, err
	}
	if !compiledManifestsEqual(stdoutManifest, artifact.Manifest) {
		return CompiledArtifact{}, fmt.Errorf("compiler stdout manifest does not match %s", artifact.ManifestPath)
	}
	return artifact, nil
}

func (c PythonCompiler) prepare(ctx context.Context, bundle Bundle) (string, error) {
	if ctx == nil {
		return "", fmt.Errorf("compilation context is nil")
	}
	if strings.TrimSpace(c.Python) == "" {
		return "", fmt.Errorf("Python compiler executable is empty")
	}
	outputRoot, err := existingDirectory(c.OutputRoot)
	if err != nil {
		return "", fmt.Errorf("resolve compiler output root: %w", err)
	}
	output := filepath.Join(outputRoot, bundle.Config.Metadata.Name)
	info, err := os.Lstat(output)
	if err == nil && !info.IsDir() {
		return "", fmt.Errorf("compiler output %s must be a directory, not a symlink or file", output)
	}
	if err != nil && !os.IsNotExist(err) {
		return "", fmt.Errorf("inspect compiler output %s: %w", output, err)
	}
	return output, nil
}

func compilerEnvironment(configured map[string]string) []string {
	environment := append([]string{}, os.Environ()...)
	keys := make([]string, 0, len(configured))
	for key := range configured {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		environment = append(environment, key+"="+configured[key])
	}
	return append(environment, "PYTHONDONTWRITEBYTECODE=1")
}

func existingDirectory(directory string) (string, error) {
	if strings.TrimSpace(directory) == "" {
		return "", fmt.Errorf("directory is required")
	}
	absolute, err := filepath.Abs(directory)
	if err != nil {
		return "", err
	}
	resolved, err := filepath.EvalSymlinks(absolute)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return "", err
	}
	if !info.IsDir() {
		return "", fmt.Errorf("%s is not a directory", resolved)
	}
	return resolved, nil
}

func decodeCompiledManifest(reader io.Reader) (CompiledManifest, error) {
	var manifest CompiledManifest
	decoder := json.NewDecoder(reader)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return CompiledManifest{}, err
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return CompiledManifest{}, fmt.Errorf("multiple JSON values are not allowed")
		}
		return CompiledManifest{}, fmt.Errorf("trailing data: %w", err)
	}
	return manifest, nil
}

func loadCompiledArtifact(directory string, source Bundle) (CompiledArtifact, error) {
	directory, err := existingDirectory(directory)
	if err != nil {
		return CompiledArtifact{}, fmt.Errorf("resolve compiled artifact: %w", err)
	}
	manifestPath, err := containedRegularFile(directory, filepath.Join(directory, compiledManifestFilename))
	if err != nil {
		return CompiledArtifact{}, fmt.Errorf("invalid compiled manifest: %w", err)
	}
	manifestFile, err := os.Open(manifestPath)
	if err != nil {
		return CompiledArtifact{}, fmt.Errorf("open compiled manifest: %w", err)
	}
	manifest, decodeErr := decodeCompiledManifest(manifestFile)
	closeErr := manifestFile.Close()
	if decodeErr != nil {
		return CompiledArtifact{}, fmt.Errorf("decode compiled manifest: %w", decodeErr)
	}
	if closeErr != nil {
		return CompiledArtifact{}, fmt.Errorf("close compiled manifest: %w", closeErr)
	}
	if err := validateCompiledManifest(directory, source, manifest); err != nil {
		return CompiledArtifact{}, err
	}
	return CompiledArtifact{Directory: directory, ManifestPath: manifestPath, Manifest: manifest}, nil
}

func validateCompiledManifest(directory string, source Bundle, manifest CompiledManifest) error {
	if err := validateCompiledServerConfig(directory); err != nil {
		return err
	}
	if err := validateCompiledIdentity(source, manifest); err != nil {
		return err
	}
	if err := validateCompiledSource(directory, source, manifest); err != nil {
		return err
	}
	seen, err := validateCompiledFiles(directory, manifest.Files)
	if err != nil {
		return err
	}
	if err := requireCompiledFiles(seen); err != nil {
		return err
	}
	if expected := compiledManifestDigest(manifest.Files); manifest.Digest != expected {
		return fmt.Errorf("compiled manifest digest %q does not match %q", manifest.Digest, expected)
	}
	return validateCompiledFileSet(directory, seen)
}

func validateCompiledServerConfig(directory string) error {
	serverConfig := filepath.Join(directory, compiledServerConfigFilename)
	if _, err := containedRegularFile(directory, serverConfig); err != nil {
		return fmt.Errorf("invalid compiled server config: %w", err)
	}
	return nil
}

func validateCompiledIdentity(source Bundle, manifest CompiledManifest) error {
	if manifest.APIVersion != APIVersion || manifest.Kind != "CompiledAgent" {
		return fmt.Errorf("compiled manifest has unsupported apiVersion/kind %q/%q", manifest.APIVersion, manifest.Kind)
	}
	// The framework runtime name is a Python identifier, while the deployment name in
	// config.yaml is a DNS label and may contain hyphens. They are deliberately
	// separate identities; the deployment continues to be keyed by metadata.name.
	if !adkAgentNamePattern.MatchString(manifest.Name) {
		return fmt.Errorf("compiled manifest name %q is not a valid agent runtime name", manifest.Name)
	}
	if manifest.Framework.Name != source.Config.Spec.Framework.Name ||
		manifest.Framework.EffectiveMode() != source.Config.Spec.Framework.EffectiveMode() {
		return fmt.Errorf(
			"compiled manifest framework %s/%s does not match config %s/%s",
			manifest.Framework.Name,
			manifest.Framework.EffectiveMode(),
			source.Config.Spec.Framework.Name,
			source.Config.Spec.Framework.EffectiveMode(),
		)
	}
	if err := validateCompiledCompatibility(manifest); err != nil {
		return err
	}
	if manifest.Entrypoint != "agent:root_agent" {
		return fmt.Errorf("compiled manifest entrypoint must be agent:root_agent, got %q", manifest.Entrypoint)
	}
	if manifest.SourceEntrypoint != source.Config.Spec.Entrypoint {
		return fmt.Errorf("compiled manifest sourceEntrypoint %q does not match %q", manifest.SourceEntrypoint, source.Config.Spec.Entrypoint)
	}
	if manifest.SourceDirectory != "source" {
		return fmt.Errorf("compiled manifest sourceDirectory must be source, got %q", manifest.SourceDirectory)
	}
	return nil
}

func validateCompiledCompatibility(manifest CompiledManifest) error {
	if strings.TrimSpace(manifest.HarnestVersion) == "" {
		return fmt.Errorf("compiled manifest harnestVersion cannot be empty")
	}
	// The package identity is retained separately from the framework name so an
	// engine can diagnose dependency drift without importing the Python runtime.
	expectedDistribution := map[string]string{"adk": "google-adk", "langgraph": "langgraph"}[manifest.Framework.Name]
	if manifest.Framework.Distribution != expectedDistribution {
		return fmt.Errorf("compiled manifest framework distribution %q does not match %q", manifest.Framework.Distribution, expectedDistribution)
	}
	if strings.TrimSpace(manifest.Framework.Version) == "" {
		return fmt.Errorf("compiled manifest framework version cannot be empty")
	}
	return nil
}

func validateCompiledSource(directory string, source Bundle, manifest CompiledManifest) error {
	compiledSource, err := existingDirectory(filepath.Join(directory, filepath.FromSlash(manifest.SourceDirectory)))
	if err != nil {
		return fmt.Errorf("resolve compiled source directory: %w", err)
	}
	compiledSourceDigest, err := digestDirectory(compiledSource)
	if err != nil {
		return fmt.Errorf("digest compiled source directory: %w", err)
	}
	if compiledSourceDigest != source.Digest {
		return fmt.Errorf("compiled source digest %q does not match source bundle %q", compiledSourceDigest, source.Digest)
	}
	return nil
}

func validateCompiledFiles(directory string, files []CompiledFile) (map[string]struct{}, error) {
	if len(files) == 0 {
		return nil, fmt.Errorf("compiled manifest files cannot be empty")
	}
	seen := make(map[string]struct{}, len(files))
	previous := ""
	for index, record := range files {
		if err := validateCompiledRecordMetadata(index, previous, record); err != nil {
			return nil, err
		}
		previous = record.Path
		seen[record.Path] = struct{}{}
		if err := validateCompiledRecordFile(directory, record); err != nil {
			return nil, err
		}
	}
	return seen, nil
}

func validateCompiledRecordMetadata(index int, previous string, record CompiledFile) error {
	if !validCompiledPath(record.Path) {
		return fmt.Errorf("compiled manifest file %d has unsafe path %q", index, record.Path)
	}
	if index > 0 && record.Path <= previous {
		return fmt.Errorf("compiled manifest files must be strictly sorted by path")
	}
	if len(record.SHA256) != 64 {
		return fmt.Errorf("compiled manifest file %q has invalid sha256", record.Path)
	}
	if _, err := hex.DecodeString(record.SHA256); err != nil {
		return fmt.Errorf("compiled manifest file %q has invalid sha256: %w", record.Path, err)
	}
	if record.Size < 0 {
		return fmt.Errorf("compiled manifest file %q has negative size", record.Path)
	}
	return nil
}

func validateCompiledRecordFile(directory string, record CompiledFile) error {
	filePath, err := containedRegularFile(directory, filepath.Join(directory, filepath.FromSlash(record.Path)))
	if err != nil {
		return fmt.Errorf("invalid compiled file %q: %w", record.Path, err)
	}
	actualHash, actualSize, err := hashFile(filePath)
	if err != nil {
		return err
	}
	if actualHash != record.SHA256 || actualSize != record.Size {
		return fmt.Errorf("compiled file %q does not match manifest hash and size", record.Path)
	}
	return nil
}

func requireCompiledFiles(seen map[string]struct{}) error {
	for _, required := range []string{"__init__.py", "agent.py", "source/config.yaml", "source/agent-card.yaml", "source/instructions.md"} {
		if _, exists := seen[required]; !exists {
			return fmt.Errorf("compiled manifest is missing required file %q", required)
		}
	}
	return nil
}

func compiledManifestDigest(files []CompiledFile) string {
	aggregate := sha256.New()
	for _, record := range files {
		_, _ = io.WriteString(aggregate, record.Path)
		_, _ = io.WriteString(aggregate, "\x00")
		_, _ = io.WriteString(aggregate, record.SHA256)
		_, _ = io.WriteString(aggregate, "\x00")
		_, _ = io.WriteString(aggregate, strconv.FormatInt(record.Size, 10))
		_, _ = io.WriteString(aggregate, "\n")
	}
	return "sha256:" + hex.EncodeToString(aggregate.Sum(nil))
}

func validCompiledPath(value string) bool {
	return value != "" && value != "." && !path.IsAbs(value) && path.Clean(value) == value &&
		!strings.HasPrefix(value, "../") && !strings.Contains(value, "\\") &&
		value != compiledManifestFilename && value != compiledServerConfigFilename
}

func hashFile(filePath string) (string, int64, error) {
	file, err := os.Open(filePath)
	if err != nil {
		return "", 0, fmt.Errorf("open compiled file %s: %w", filePath, err)
	}
	hash := sha256.New()
	size, copyErr := io.Copy(hash, file)
	closeErr := file.Close()
	if copyErr != nil {
		return "", 0, fmt.Errorf("hash compiled file %s: %w", filePath, copyErr)
	}
	if closeErr != nil {
		return "", 0, fmt.Errorf("close compiled file %s: %w", filePath, closeErr)
	}
	return hex.EncodeToString(hash.Sum(nil)), size, nil
}

func validateCompiledFileSet(directory string, expected map[string]struct{}) error {
	actual := make(map[string]struct{}, len(expected))
	err := filepath.WalkDir(directory, collectCompiledFile(directory, actual))
	if err != nil {
		return fmt.Errorf("inspect compiled artifact: %w", err)
	}
	for filePath := range actual {
		if _, exists := expected[filePath]; !exists {
			return fmt.Errorf("compiled artifact contains unmanifested file %q", filePath)
		}
	}
	return nil
}

func collectCompiledFile(directory string, actual map[string]struct{}) fs.WalkDirFunc {
	return func(filePath string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if filePath == directory || entry.IsDir() {
			return nil
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("compiled artifact contains symlink %s", filePath)
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("compiled artifact contains non-regular file %s", filePath)
		}
		relative, err := filepath.Rel(directory, filePath)
		if err != nil {
			return err
		}
		relative = filepath.ToSlash(relative)
		// server.yaml is intentionally mutable deployment policy. Every other
		// runtime file remains immutable and covered by the manifest digest.
		if relative != compiledManifestFilename && relative != compiledServerConfigFilename {
			actual[relative] = struct{}{}
		}
		return nil
	}
}

func compiledManifestsEqual(left, right CompiledManifest) bool {
	leftJSON, _ := json.Marshal(left)
	rightJSON, _ := json.Marshal(right)
	return bytes.Equal(leftJSON, rightJSON)
}
