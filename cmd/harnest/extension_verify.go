package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
)

const (
	extensionEntryPointGroup    = "harnest.extensions"
	extensionInspectionVersion  = 3
	maxExtensionWheelBytes      = 16 * 1024 * 1024
	maxExtensionWheelFiles      = 10_000
	maxExtensionMetadataEntry   = 1024 * 1024
	maxExtensionInspectionBytes = 16 * 1024
)

// Add names only after Fused controls the corresponding public PyPI project.
var officialPyPIExtensionProjects = []string{
	"harnest-extension-docker",
	"harnest-extension-hatchet",
	"harnest-extension-rag",
}

type pypiReleaseFile struct {
	Filename    string `json:"filename"`
	PackageType string `json:"packagetype"`
	URL         string `json:"url"`
	Size        int64  `json:"size"`
	Yanked      bool   `json:"yanked"`
	Digests     struct {
		SHA256 string `json:"sha256"`
	} `json:"digests"`
}

type extensionInspection struct {
	Version    int    `json:"version"`
	Name       string `json:"name"`
	Release    string `json:"release"`
	SHA256     string `json:"sha256"`
	Compatible bool   `json:"compatible"`
}

type extensionEntryPoint struct {
	Name  string
	Value string
}

type extensionWheelPackage struct {
	EntryPoint extensionEntryPoint
	Manifest   []byte
}

// inspectPyPIExtension checks compatibility without importing package code.
func (a *application) inspectPyPIExtension(
	ctx context.Context, name string, metadata pypiProjectMetadata,
) (extensionInspection, error) {
	if normalizeProjectName(metadata.Info.Name) != normalizeProjectName(name) ||
		!safeExtensionMetadataValue(metadata.Info.Version, 50) {
		return extensionInspection{}, fmt.Errorf("PyPI metadata identity does not match project")
	}
	artifact, err := selectExtensionWheel(metadata.URLs)
	if err != nil {
		return extensionInspection{}, err
	}
	if cached, found := a.readExtensionInspection(name, metadata.Info.Version, artifact); found {
		return cached, nil
	}
	contents, err := a.downloadExtensionWheel(ctx, artifact)
	if err != nil {
		return extensionInspection{}, err
	}
	inspection := extensionInspection{
		Version: extensionInspectionVersion, Name: name,
		Release: metadata.Info.Version, SHA256: artifact.Digests.SHA256,
		Compatible: inspectExtensionWheel(contents, name, metadata.Info.Version) == nil,
	}
	a.writeExtensionInspection(inspection)
	return inspection, nil
}

// extensionProjectTrust applies the explicit Fused ownership policy.
func extensionProjectTrust(name string) string {
	return classifyExtensionProject(name, officialPyPIExtensionProjects)
}

func classifyExtensionProject(name string, officialProjects []string) string {
	for _, official := range officialProjects {
		if normalizeProjectName(name) == normalizeProjectName(official) {
			return "official"
		}
	}
	return "community"
}

// selectExtensionWheel chooses the smallest bounded universal wheel.
func selectExtensionWheel(files []pypiReleaseFile) (pypiReleaseFile, error) {
	wheels := make([]pypiReleaseFile, 0, len(files))
	for _, file := range files {
		if file.PackageType == "bdist_wheel" && !file.Yanked &&
			validUniversalWheelFilename(file.Filename) && file.Size > 0 &&
			file.Size <= maxExtensionWheelBytes && validSHA256(file.Digests.SHA256) {
			wheels = append(wheels, file)
		}
	}
	if len(wheels) == 0 {
		return pypiReleaseFile{}, fmt.Errorf(
			"latest release has no inspectable universal py3-none-any wheel",
		)
	}
	sort.Slice(wheels, func(i, j int) bool {
		if wheels[i].Size != wheels[j].Size {
			return wheels[i].Size < wheels[j].Size
		}
		return wheels[i].Filename < wheels[j].Filename
	})
	return wheels[0], nil
}

// validUniversalWheelFilename limits installation to artifacts safe on every target.
func validUniversalWheelFilename(value string) bool {
	if !validWheelFilename(value) {
		return false
	}
	parts := strings.Split(strings.TrimSuffix(value, path.Ext(value)), "-")
	return len(parts) >= 5 && parts[len(parts)-3] == "py3" &&
		parts[len(parts)-2] == "none" && parts[len(parts)-1] == "any"
}

func validWheelFilename(value string) bool {
	return len(value) > 4 && len(value) <= 300 && path.Base(value) == value &&
		!strings.Contains(value, "\\") && safeExtensionMetadataValue(value, 300) &&
		strings.HasSuffix(strings.ToLower(value), ".whl")
}

func validSHA256(value string) bool {
	decoded, err := hex.DecodeString(value)
	return err == nil && len(decoded) == sha256.Size && value == strings.ToLower(value)
}

// downloadExtensionWheel bounds the transfer and checks PyPI's published digest.
func (a *application) downloadExtensionWheel(
	ctx context.Context, artifact pypiReleaseFile,
) ([]byte, error) {
	location, err := a.validExtensionArtifactURL(artifact.URL)
	if err != nil {
		return nil, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, location.String(), nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("User-Agent", "harnest/"+a.version)
	response, err := a.extensionHTTPClient().Do(request)
	if err != nil {
		return nil, fmt.Errorf("download extension wheel: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || response.ContentLength > maxExtensionWheelBytes {
		return nil, fmt.Errorf("download extension wheel: HTTP %d", response.StatusCode)
	}
	contents, err := io.ReadAll(io.LimitReader(response.Body, maxExtensionWheelBytes+1))
	if err != nil || len(contents) > maxExtensionWheelBytes {
		return nil, fmt.Errorf("read bounded extension wheel")
	}
	digest := sha256.Sum256(contents)
	if int64(len(contents)) != artifact.Size ||
		hex.EncodeToString(digest[:]) != artifact.Digests.SHA256 {
		return nil, fmt.Errorf("extension wheel does not match PyPI metadata")
	}
	return contents, nil
}

// validExtensionArtifactURL prevents project metadata from selecting another host.
func (a *application) validExtensionArtifactURL(value string) (*url.URL, error) {
	location, err := url.Parse(value)
	if err != nil || location.Scheme != "https" || location.User != nil ||
		location.Hostname() == "" || location.RawQuery != "" || location.Fragment != "" {
		return nil, fmt.Errorf("PyPI returned an unsafe extension wheel URL")
	}
	base, _ := url.Parse(a.system.pypiBaseURL)
	allowed := strings.EqualFold(location.Hostname(), base.Hostname())
	if strings.EqualFold(base.Hostname(), "pypi.org") {
		allowed = strings.EqualFold(location.Hostname(), "files.pythonhosted.org")
	}
	if !allowed {
		return nil, fmt.Errorf("PyPI returned a extension wheel on an untrusted host")
	}
	return location, nil
}

// inspectExtensionWheel binds the package name to one entry point and manifest.
func inspectExtensionWheel(contents []byte, projectName, release string) error {
	_, err := readExtensionWheelPackage(contents, projectName, release)
	return err
}

// readExtensionWheelPackage returns only the verified resources needed by installation.
func readExtensionWheelPackage(
	contents []byte, projectName, release string,
) (extensionWheelPackage, error) {
	reader, err := zip.NewReader(bytes.NewReader(contents), int64(len(contents)))
	if err != nil || len(reader.File) > maxExtensionWheelFiles {
		return extensionWheelPackage{}, fmt.Errorf("invalid or oversized extension wheel")
	}
	entrypointFile, err := findExtensionEntryPointFile(reader.File)
	if err != nil {
		return extensionWheelPackage{}, err
	}
	entrypointBytes, err := readExtensionWheelMetadata(entrypointFile)
	if err != nil {
		return extensionWheelPackage{}, err
	}
	entrypoint, err := parseExtensionEntryPointFile(string(entrypointBytes))
	if err != nil {
		return extensionWheelPackage{}, err
	}
	manifest, err := validateExtensionWheelContent(reader.File, entrypoint, projectName, release)
	if err != nil {
		return extensionWheelPackage{}, err
	}
	return extensionWheelPackage{EntryPoint: entrypoint, Manifest: manifest}, nil
}

// validateExtensionWheelContent checks the three resources that define compatibility.
func validateExtensionWheelContent(
	files []*zip.File, entrypoint extensionEntryPoint, projectName, release string,
) ([]byte, error) {
	slug := extensionProjectSlug(projectName)
	if normalizeProjectName(entrypoint.Name) != slug {
		return nil, fmt.Errorf("extension entry point name does not match project name")
	}
	root, err := extensionModuleRoot(entrypoint.Value)
	if err != nil {
		return nil, err
	}
	stem, kind := extensionWheelFormat(entrypoint.Value)
	manifestFile, err := findUniqueWheelFile(files, root+"/"+stem+".yaml")
	if err != nil {
		return nil, err
	}
	if _, err := findUniqueWheelFile(files, root+"/"+stem+".py"); err != nil {
		return nil, err
	}
	manifestBytes, err := readExtensionWheelMetadata(manifestFile)
	if err != nil {
		return nil, err
	}
	if err := validateExtensionWheelManifest(
		manifestBytes, entrypoint.Name, release, kind, stem+":"+stem,
	); err != nil {
		return nil, err
	}
	return manifestBytes, nil
}

// findExtensionEntryPointFile requires one unambiguous distribution metadata file.
func findExtensionEntryPointFile(files []*zip.File) (*zip.File, error) {
	var match *zip.File
	for _, file := range files {
		if strings.HasSuffix(file.Name, ".dist-info/entry_points.txt") {
			if match != nil {
				return nil, fmt.Errorf("extension wheel has ambiguous metadata")
			}
			match = file
		}
	}
	if match == nil {
		return nil, fmt.Errorf("extension wheel has no entry points")
	}
	return match, nil
}

// findUniqueWheelFile locates required content without extracting the archive.
func findUniqueWheelFile(files []*zip.File, name string) (*zip.File, error) {
	var match *zip.File
	for _, file := range files {
		if file.Name == name {
			if match != nil {
				return nil, fmt.Errorf("extension wheel contains duplicate files")
			}
			match = file
		}
	}
	if match == nil {
		return nil, fmt.Errorf("extension wheel is missing %s", path.Base(name))
	}
	return match, nil
}

// readExtensionWheelMetadata bounds decompression of the two inspected resources.
func readExtensionWheelMetadata(file *zip.File) ([]byte, error) {
	reader, err := file.Open()
	if err != nil {
		return nil, err
	}
	defer reader.Close()
	contents, err := io.ReadAll(io.LimitReader(reader, maxExtensionMetadataEntry+1))
	if err != nil || len(contents) > maxExtensionMetadataEntry {
		return nil, fmt.Errorf("extension wheel metadata exceeds its limit")
	}
	return contents, nil
}

// parseExtensionEntryPointFile reads the extension group emitted by wheels.
func parseExtensionEntryPointFile(contents string) (extensionEntryPoint, error) {
	return parseWheelEntryPoint(contents, extensionEntryPointGroup, extensionWheelFormat)
}

// parseWheelEntryPoint reads exactly one entry from one INI group in a wheel.
func parseWheelEntryPoint(
	contents, group string, format func(string) (string, string),
) (extensionEntryPoint, error) {
	section := ""
	entries := []extensionEntryPoint{}
	for _, raw := range strings.Split(contents, "\n") {
		line := strings.TrimSpace(raw)
		if ignoredExtensionEntryPointLine(line) {
			continue
		}
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			section = strings.TrimSpace(line[1 : len(line)-1])
			continue
		}
		if section != group {
			continue
		}
		name, value, found := strings.Cut(line, "=")
		if !found {
			return extensionEntryPoint{}, fmt.Errorf("invalid Harnest entry point")
		}
		stem, _ := format(strings.TrimSpace(value))
		if stem == "" {
			return extensionEntryPoint{}, fmt.Errorf("Harnest entry point group does not match its format")
		}
		entries = append(entries, extensionEntryPoint{
			Name: strings.TrimSpace(name), Value: strings.TrimSpace(value),
		})
	}
	if len(entries) != 1 || !validPythonIdentifier(entries[0].Name) {
		return extensionEntryPoint{}, fmt.Errorf("wheel must declare one Harnest entry point")
	}
	return entries[0], nil
}

func ignoredExtensionEntryPointLine(line string) bool {
	return line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, ";")
}

// extensionModuleRoot binds the standard entry point to the fixed runtime object.
func extensionModuleRoot(value string) (string, error) {
	module, attribute, found := strings.Cut(value, ":")
	if !found || attribute != "extension" || strings.ContainsAny(value, " []") {
		return "", fmt.Errorf("Harnest entry point must end in .extension:extension")
	}
	parts := strings.Split(module, ".")
	for _, part := range parts {
		if !validPythonIdentifier(part) {
			return "", fmt.Errorf("Harnest entry point must name a Python module")
		}
	}
	if len(parts) < 2 || parts[len(parts)-1] != attribute {
		return "", fmt.Errorf("Harnest entry point module must match its singleton name")
	}
	return strings.Join(parts[:len(parts)-1], "/"), nil
}

func validPythonIdentifier(value string) bool {
	if value == "" || !asciiIdentifierStart(value[0]) {
		return false
	}
	for index := 1; index < len(value); index++ {
		if !asciiIdentifierStart(value[index]) && (value[index] < '0' || value[index] > '9') {
			return false
		}
	}
	return true
}

func asciiIdentifierStart(value byte) bool {
	return value == '_' || value >= 'a' && value <= 'z' || value >= 'A' && value <= 'Z'
}

// validateExtensionWheelManifest binds runtime identity without full installation checks.
func validateExtensionWheelManifest(contents []byte, entryName, release, kind, entrypoint string) error {
	manifest, err := decodeLocalExtensionManifest(contents)
	if err != nil {
		return fmt.Errorf("invalid extension manifest")
	}
	if err := validateLocalExtensionManifest(manifest); err != nil {
		return fmt.Errorf("invalid extension manifest")
	}
	if manifest.APIVersion != "harnest.dev/v1alpha1" ||
		manifest.Kind != kind || manifest.Runtime == nil || manifest.Runtime.Entrypoint != entrypoint ||
		manifest.Metadata == nil || manifest.Metadata.Name != entryName || manifest.Metadata.Version != release {
		return fmt.Errorf("extension manifest identity does not match its distribution")
	}
	return nil
}

func safeExtensionMetadataValue(value string, limit int) bool {
	if value == "" || len(value) > limit {
		return false
	}
	for _, character := range value {
		if character < 0x20 || character > 0x7e {
			return false
		}
	}
	return true
}

// readExtensionInspection reuses compatibility decisions for immutable artifacts.
func (a *application) readExtensionInspection(
	name, release string, artifact pypiReleaseFile,
) (extensionInspection, bool) {
	path, err := a.extensionInspectionPath(artifact.Digests.SHA256)
	if err != nil {
		return extensionInspection{}, false
	}
	contents, found := readExtensionInspectionFile(path)
	if !found {
		return extensionInspection{}, false
	}
	var inspection extensionInspection
	if json.Unmarshal(contents, &inspection) != nil || !validExtensionInspection(
		inspection, name, release, artifact.Digests.SHA256,
	) {
		return extensionInspection{}, false
	}
	return inspection, true
}

// readExtensionInspectionFile rejects links and oversized cache entries.
func readExtensionInspectionFile(path string) ([]byte, bool) {
	info, err := os.Lstat(path)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() ||
		info.Size() > maxExtensionInspectionBytes {
		return nil, false
	}
	contents, err := os.ReadFile(path)
	return contents, err == nil
}

// validExtensionInspection prevents decisions crossing immutable artifacts.
func validExtensionInspection(
	inspection extensionInspection, name, release, digest string,
) bool {
	return inspection.Version == extensionInspectionVersion &&
		normalizeProjectName(inspection.Name) == normalizeProjectName(name) &&
		inspection.Release == release && inspection.SHA256 == digest
}

// extensionInspectionPath keys immutable decisions by artifact digest.
func (a *application) extensionInspectionPath(digest string) (string, error) {
	cacheDirectory := a.system.userCacheDir
	if cacheDirectory == nil {
		cacheDirectory = os.UserCacheDir
	}
	root, err := cacheDirectory()
	if err != nil {
		return "", fmt.Errorf("resolve user cache directory: %w", err)
	}
	return filepath.Join(root, "harnest", "extensions", "inspections", digest+".json"), nil
}

// writeExtensionInspection best-effort publishes non-executable cache state.
func (a *application) writeExtensionInspection(inspection extensionInspection) {
	path, err := a.extensionInspectionPath(inspection.SHA256)
	if err != nil {
		return
	}
	contents, err := json.Marshal(inspection)
	if err != nil {
		return
	}
	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0o755); err != nil {
		return
	}
	temporary, err := os.CreateTemp(directory, ".inspection-*.json")
	if err != nil {
		return
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(0o600); err != nil {
		temporary.Close()
		return
	}
	if _, err := temporary.Write(contents); err != nil {
		temporary.Close()
		return
	}
	if temporary.Close() == nil {
		_ = os.Rename(temporaryPath, path)
	}
}
