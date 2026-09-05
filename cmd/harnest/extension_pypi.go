package main

import (
	"archive/zip"
	"bytes"
	"context"
	"fmt"
	"io"
	"net/mail"
	"net/textproto"
	"os"
	"path"
	"path/filepath"
	"strings"

	"github.com/pelletier/go-toml/v2"
)

const maxInstalledExtensionBytes = 64 * 1024 * 1024

type pypiExtensionPackage struct {
	Manifest       localExtensionManifest
	Resources      map[string][]byte
	ProjectName    string
	Version        string
	RequiresPython string
	Dependencies   []string
}

type pypiExtensionProject struct {
	Project struct {
		Name           string   `toml:"name"`
		Version        string   `toml:"version"`
		Readme         string   `toml:"readme,omitempty"`
		RequiresPython string   `toml:"requires-python,omitempty"`
		Dependencies   []string `toml:"dependencies"`
	} `toml:"project"`
	Tool struct {
		UV struct {
			Package bool `toml:"package"`
		} `toml:"uv"`
	} `toml:"tool"`
}

// installPyPIExtension installs a non-executing loader pinned to one verified wheel.
func (a *application) installPyPIExtension(
	ctx context.Context, source, project string, force bool,
) (installedExtension, error) {
	projectName, err := canonicalExtensionProject(source)
	if err != nil {
		return installedExtension{}, err
	}
	projectRoot, err := validatedAgentProject(project)
	if err != nil {
		return installedExtension{}, err
	}
	extensionsRoot := filepath.Join(projectRoot, "extensions")
	if err := validateLocalPackageRoot(extensionsRoot, "extensions"); err != nil {
		return installedExtension{}, err
	}
	metadata, err := a.fetchPyPIPluginMetadata(ctx, projectName, false)
	if err != nil {
		return installedExtension{}, fmt.Errorf("resolve PyPI Harnest Extension %q: %w", projectName, err)
	}
	downloaded, err := a.downloadPyPIExtension(ctx, projectName, metadata)
	if err != nil {
		return installedExtension{}, err
	}
	destination := filepath.Join(extensionsRoot, downloaded.Manifest.Metadata.Name)
	if err := validateLocalPackageDestination(destination, force, "Harnest Extension"); err != nil {
		return installedExtension{}, err
	}
	staging, err := stagePyPIExtensionPackage(downloaded)
	if err != nil {
		return installedExtension{}, err
	}
	defer os.RemoveAll(staging)
	if err := installLocalPackageTree(
		staging, destination, force, "Harnest Extension", "extensions",
	); err != nil {
		return installedExtension{}, err
	}
	return installedExtension{Name: downloaded.Manifest.Metadata.Name, Path: destination}, nil
}

// canonicalExtensionProject accepts a slug while keeping installation in the canonical namespace.
func canonicalExtensionProject(source string) (string, error) {
	if !validPyPIProjectName(source) {
		return "", fmt.Errorf("PyPI Harnest Extension must be a project name or short slug")
	}
	name := normalizeProjectName(source)
	if strings.HasPrefix(name, pypiPluginPrefix) {
		return "", fmt.Errorf("legacy RuntimePlugin projects cannot be installed as Harnest Extensions")
	}
	if !strings.HasPrefix(name, pypiExtensionPrefix) {
		name = pypiExtensionPrefix + name
	}
	if extensionProjectSlug(name) == "" || !validPyPIProjectName(name) {
		return "", fmt.Errorf("invalid PyPI Harnest Extension project %q", source)
	}
	return name, nil
}

// downloadPyPIExtension binds metadata, artifact bytes, and canonical manifest identity.
func (a *application) downloadPyPIExtension(
	ctx context.Context, projectName string, metadata pypiProjectMetadata,
) (pypiExtensionPackage, error) {
	if normalizeProjectName(metadata.Info.Name) != projectName ||
		!safePluginMetadataValue(metadata.Info.Version, 50) {
		return pypiExtensionPackage{}, fmt.Errorf("PyPI metadata identity does not match project")
	}
	artifact, err := selectPluginWheel(metadata.URLs)
	if err != nil {
		return pypiExtensionPackage{}, err
	}
	contents, err := a.downloadPluginWheel(ctx, artifact)
	if err != nil {
		return pypiExtensionPackage{}, err
	}
	wheel, err := readPluginWheelPackage(contents, projectName, metadata.Info.Version)
	if err != nil {
		return pypiExtensionPackage{}, fmt.Errorf("PyPI wheel is not an installable Harnest Extension: %w", err)
	}
	stem, _ := extensionWheelFormat(wheel.EntryPoint.Value)
	if stem != "extension" {
		return pypiExtensionPackage{}, fmt.Errorf("legacy RuntimePlugin wheels cannot be installed as Harnest Extensions")
	}
	manifest, err := decodeLocalExtensionManifest(wheel.Manifest)
	if err != nil {
		return pypiExtensionPackage{}, fmt.Errorf("invalid extension.yaml: %w", err)
	}
	if err := validateLocalExtensionManifest(manifest); err != nil {
		return pypiExtensionPackage{}, fmt.Errorf("invalid extension.yaml: %w", err)
	}
	downloaded, err := readPyPIExtensionPackage(contents, projectName, metadata.Info.Version, wheel)
	if err != nil {
		return pypiExtensionPackage{}, err
	}
	downloaded.Manifest = manifest
	return downloaded, nil
}

// stagePyPIExtensionPackage materializes verified package resources as a canonical local extension.
func stagePyPIExtensionPackage(downloaded pypiExtensionPackage) (string, error) {
	root, err := os.MkdirTemp("", ".harnest-pypi-extension-")
	if err != nil {
		return "", fmt.Errorf("create PyPI Harnest Extension staging directory: %w", err)
	}
	cleanup := func(err error) (string, error) {
		_ = os.RemoveAll(root)
		return "", err
	}
	project, err := pypiExtensionProjectSource(downloaded)
	if err != nil {
		return cleanup(err)
	}
	downloaded.Resources["pyproject.toml"] = project
	for name, contents := range downloaded.Resources {
		target := filepath.Join(root, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return cleanup(fmt.Errorf("stage PyPI Harnest Extension directory: %w", err))
		}
		if err := os.WriteFile(target, contents, 0o644); err != nil {
			return cleanup(fmt.Errorf("stage PyPI Harnest Extension %s: %w", name, err))
		}
	}
	if err := validateLocalExtensionLayout(root); err != nil {
		return cleanup(err)
	}
	if err := validateLocalExtensionProject(root, downloaded.Manifest); err != nil {
		return cleanup(err)
	}
	return root, nil
}

// pypiExtensionProjectSource reconstructs static dependency metadata from the wheel.
func pypiExtensionProjectSource(downloaded pypiExtensionPackage) ([]byte, error) {
	var document pypiExtensionProject
	document.Project.Name = downloaded.ProjectName
	document.Project.Version = downloaded.Version
	if _, found := downloaded.Resources["README.md"]; found {
		// The wheel carries the authored long description as package data; keep
		// the materialized project metadata valid for local tooling as well.
		document.Project.Readme = "README.md"
	}
	document.Project.RequiresPython = downloaded.RequiresPython
	document.Project.Dependencies = downloaded.Dependencies
	contents, err := toml.Marshal(document)
	if err != nil {
		return nil, fmt.Errorf("encode installed Harnest Extension pyproject.toml: %w", err)
	}
	return contents, nil
}

// readPyPIExtensionPackage extracts only the verified module root and bounded core metadata.
func readPyPIExtensionPackage(
	contents []byte, projectName, release string, wheel pluginWheelPackage,
) (pypiExtensionPackage, error) {
	reader, err := zip.NewReader(bytes.NewReader(contents), int64(len(contents)))
	if err != nil {
		return pypiExtensionPackage{}, fmt.Errorf("read PyPI Harnest Extension wheel: %w", err)
	}
	moduleRoot, err := pluginModuleRoot(wheel.EntryPoint.Value)
	if err != nil {
		return pypiExtensionPackage{}, err
	}
	resources, err := readExtensionWheelResources(reader.File, moduleRoot)
	if err != nil {
		return pypiExtensionPackage{}, err
	}
	requiresPython, dependencies, err := readExtensionWheelProject(
		reader.File, projectName, release,
	)
	if err != nil {
		return pypiExtensionPackage{}, err
	}
	return pypiExtensionPackage{
		Resources: resources, ProjectName: projectName, Version: release,
		RequiresPython: requiresPython, Dependencies: dependencies,
	}, nil
}

// readExtensionWheelResources rejects unsafe layouts before any path reaches the filesystem.
func readExtensionWheelResources(
	files []*zip.File, moduleRoot string,
) (map[string][]byte, error) {
	resources := map[string][]byte{}
	casefoldPaths := map[string]string{}
	prefix := moduleRoot + "/"
	remaining := int64(maxInstalledExtensionBytes)
	for _, file := range files {
		if !strings.HasPrefix(file.Name, prefix) || strings.HasSuffix(file.Name, "/") {
			continue
		}
		relative := strings.TrimPrefix(file.Name, prefix)
		if relative == "__init__.py" {
			continue
		}
		if err := validateExtensionWheelResource(relative, file); err != nil {
			return nil, err
		}
		if err := validateExtensionWheelResourceIdentity(
			relative, resources, casefoldPaths,
		); err != nil {
			return nil, err
		}
		contents, err := readBoundedExtensionWheelFile(file, &remaining)
		if err != nil {
			return nil, err
		}
		resources[relative] = contents
	}
	for _, required := range []string{"extension.yaml", "extension.py"} {
		if _, found := resources[required]; !found {
			return nil, fmt.Errorf("PyPI Harnest Extension wheel is missing %s", required)
		}
	}
	return resources, nil
}

// validateExtensionWheelResourceIdentity prevents nondeterministic archive overwrites.
func validateExtensionWheelResourceIdentity(
	relative string, resources map[string][]byte, casefoldPaths map[string]string,
) error {
	casefold := strings.ToLower(relative)
	if previous, duplicate := casefoldPaths[casefold]; duplicate {
		return fmt.Errorf(
			"PyPI Harnest Extension wheel resources conflict by case: %s and %s",
			previous, relative,
		)
	}
	casefoldPaths[casefold] = relative
	if _, duplicate := resources[relative]; duplicate {
		return fmt.Errorf("PyPI Harnest Extension wheel contains duplicate resource %s", relative)
	}
	return nil
}

// validateExtensionWheelResource applies the application-local layout before extraction.
func validateExtensionWheelResource(relative string, file *zip.File) error {
	if !safeExtensionWheelResourcePath(relative) {
		return fmt.Errorf("PyPI Harnest Extension wheel contains an unsafe resource path")
	}
	root, _, _ := strings.Cut(relative, "/")
	expectsFile, allowed := extensionRootEntries[root]
	if !allowed || expectsFile && relative != root {
		return fmt.Errorf("unexpected Harnest Extension resource in wheel: %s", relative)
	}
	if file.Mode()&os.ModeSymlink != 0 || !file.Mode().IsRegular() {
		return fmt.Errorf("PyPI Harnest Extension resources must be regular files: %s", relative)
	}
	if file.UncompressedSize64 > uint64(maxInstalledExtensionBytes) {
		return fmt.Errorf("PyPI Harnest Extension resource exceeds the installed-size limit: %s", relative)
	}
	return nil
}

// safeExtensionWheelResourcePath confines portable archive paths to the module root.
func safeExtensionWheelResourcePath(relative string) bool {
	return safePluginMetadataValue(relative, 1_000) &&
		!strings.Contains(relative, "\\") && !path.IsAbs(relative) &&
		path.Clean(relative) == relative && !strings.HasPrefix(relative, "../")
}

// readBoundedExtensionWheelFile prevents compressed entries from expanding past the package cap.
func readBoundedExtensionWheelFile(file *zip.File, remaining *int64) ([]byte, error) {
	if *remaining <= 0 || file.UncompressedSize64 > uint64(*remaining) {
		return nil, fmt.Errorf("PyPI Harnest Extension exceeds the installed-size limit")
	}
	reader, err := file.Open()
	if err != nil {
		return nil, fmt.Errorf("read PyPI Harnest Extension resource: %w", err)
	}
	defer reader.Close()
	contents, err := io.ReadAll(io.LimitReader(reader, *remaining+1))
	if err != nil || int64(len(contents)) > *remaining {
		return nil, fmt.Errorf("read bounded PyPI Harnest Extension resource")
	}
	*remaining -= int64(len(contents))
	return contents, nil
}

// readExtensionWheelProject retains the distribution's static Python and dependency contract.
func readExtensionWheelProject(
	files []*zip.File, projectName, release string,
) (string, []string, error) {
	metadataFile, err := findExtensionWheelMetadata(files)
	if err != nil {
		return "", nil, err
	}
	contents, err := readPluginWheelMetadata(metadataFile)
	if err != nil {
		return "", nil, err
	}
	message, err := mail.ReadMessage(bytes.NewReader(contents))
	if err != nil {
		return "", nil, fmt.Errorf("invalid PyPI Harnest Extension METADATA")
	}
	if normalizeProjectName(message.Header.Get("Name")) != projectName ||
		message.Header.Get("Version") != release {
		return "", nil, fmt.Errorf("PyPI Harnest Extension METADATA identity does not match its release")
	}
	requiresPython := message.Header.Get("Requires-Python")
	if requiresPython != "" && !safePluginMetadataValue(requiresPython, 500) {
		return "", nil, fmt.Errorf("invalid PyPI Harnest Extension Requires-Python metadata")
	}
	dependencies := append(
		[]string(nil), message.Header[textproto.CanonicalMIMEHeaderKey("Requires-Dist")]...,
	)
	if err := validateExtensionWheelDependencies(dependencies); err != nil {
		return "", nil, err
	}
	return requiresPython, dependencies, nil
}

// validateExtensionWheelDependencies bounds static requirements before TOML generation.
func validateExtensionWheelDependencies(dependencies []string) error {
	if len(dependencies) > 256 {
		return fmt.Errorf("PyPI Harnest Extension declares too many dependencies")
	}
	for _, dependency := range dependencies {
		if !safePluginMetadataValue(dependency, 2_000) {
			return fmt.Errorf("invalid PyPI Harnest Extension dependency metadata")
		}
	}
	return nil
}

// findExtensionWheelMetadata requires one unambiguous core metadata document.
func findExtensionWheelMetadata(files []*zip.File) (*zip.File, error) {
	var found *zip.File
	for _, file := range files {
		if !strings.HasSuffix(strings.ToLower(file.Name), ".dist-info/metadata") {
			continue
		}
		if found != nil {
			return nil, fmt.Errorf("PyPI Harnest Extension wheel has ambiguous METADATA")
		}
		found = file
	}
	if found == nil {
		return nil, fmt.Errorf("PyPI Harnest Extension wheel has no METADATA")
	}
	return found, nil
}
