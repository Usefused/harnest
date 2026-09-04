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

	"gopkg.in/yaml.v3"
)

const (
	pluginEntryPointGroup    = "harnest.plugins"
	pluginInspectionVersion  = 3
	maxPluginWheelBytes      = 16 * 1024 * 1024
	maxPluginWheelFiles      = 10_000
	maxPluginMetadataEntry   = 1024 * 1024
	maxPluginInspectionBytes = 16 * 1024
)

// Add names only after Fused controls the corresponding public PyPI project.
var officialPyPIPluginProjects = []string{}

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

type pluginInspection struct {
	Version    int    `json:"version"`
	Name       string `json:"name"`
	Release    string `json:"release"`
	SHA256     string `json:"sha256"`
	Compatible bool   `json:"compatible"`
}

type pluginWheelManifest struct {
	APIVersion string `yaml:"apiVersion"`
	Kind       string `yaml:"kind"`
	Metadata   struct {
		Name    string `yaml:"name"`
		Version string `yaml:"version"`
	} `yaml:"metadata"`
	Runtime struct {
		Entrypoint string `yaml:"entrypoint"`
	} `yaml:"runtime"`
	Requires struct {
		Plugins    []string `yaml:"plugins"`
		Extensions []string `yaml:"extensions"`
	} `yaml:"requires"`
	Capabilities []string `yaml:"capabilities"`
}

type pluginEntryPoint struct {
	Name  string
	Value string
}

// inspectPyPIPlugin checks compatibility without importing package code.
func (a *application) inspectPyPIPlugin(
	ctx context.Context, name string, metadata pypiProjectMetadata,
) (pluginInspection, error) {
	if normalizeProjectName(metadata.Info.Name) != normalizeProjectName(name) ||
		!safePluginMetadataValue(metadata.Info.Version, 50) {
		return pluginInspection{}, fmt.Errorf("PyPI metadata identity does not match project")
	}
	artifact, err := selectPluginWheel(metadata.URLs)
	if err != nil {
		return pluginInspection{}, err
	}
	if cached, found := a.readPluginInspection(name, metadata.Info.Version, artifact); found {
		return cached, nil
	}
	contents, err := a.downloadPluginWheel(ctx, artifact)
	if err != nil {
		return pluginInspection{}, err
	}
	inspection := pluginInspection{
		Version: pluginInspectionVersion, Name: name,
		Release: metadata.Info.Version, SHA256: artifact.Digests.SHA256,
		Compatible: inspectPluginWheel(contents, name, metadata.Info.Version) == nil,
	}
	a.writePluginInspection(inspection)
	return inspection, nil
}

// pluginProjectTrust applies the explicit Fused ownership policy.
func pluginProjectTrust(name string) string {
	return classifyPluginProject(name, officialPyPIPluginProjects)
}

func classifyPluginProject(name string, officialProjects []string) string {
	for _, official := range officialProjects {
		if normalizeProjectName(name) == normalizeProjectName(official) {
			return "official"
		}
	}
	return "community"
}

// selectPluginWheel chooses the smallest bounded non-yanked wheel.
func selectPluginWheel(files []pypiReleaseFile) (pypiReleaseFile, error) {
	wheels := make([]pypiReleaseFile, 0, len(files))
	for _, file := range files {
		if file.PackageType == "bdist_wheel" && !file.Yanked &&
			validWheelFilename(file.Filename) && file.Size > 0 &&
			file.Size <= maxPluginWheelBytes && validSHA256(file.Digests.SHA256) {
			wheels = append(wheels, file)
		}
	}
	if len(wheels) == 0 {
		return pypiReleaseFile{}, fmt.Errorf("latest release has no inspectable wheel")
	}
	sort.Slice(wheels, func(i, j int) bool {
		if wheels[i].Size != wheels[j].Size {
			return wheels[i].Size < wheels[j].Size
		}
		return wheels[i].Filename < wheels[j].Filename
	})
	return wheels[0], nil
}

func validWheelFilename(value string) bool {
	return len(value) > 4 && len(value) <= 300 && path.Base(value) == value &&
		!strings.Contains(value, "\\") && safePluginMetadataValue(value, 300) &&
		strings.HasSuffix(strings.ToLower(value), ".whl")
}

func validSHA256(value string) bool {
	decoded, err := hex.DecodeString(value)
	return err == nil && len(decoded) == sha256.Size && value == strings.ToLower(value)
}

// downloadPluginWheel bounds the transfer and checks PyPI's published digest.
func (a *application) downloadPluginWheel(
	ctx context.Context, artifact pypiReleaseFile,
) ([]byte, error) {
	location, err := a.validPluginArtifactURL(artifact.URL)
	if err != nil {
		return nil, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, location.String(), nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("User-Agent", "harnest/"+a.version)
	response, err := a.pluginHTTPClient().Do(request)
	if err != nil {
		return nil, fmt.Errorf("download plugin wheel: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || response.ContentLength > maxPluginWheelBytes {
		return nil, fmt.Errorf("download plugin wheel: HTTP %d", response.StatusCode)
	}
	contents, err := io.ReadAll(io.LimitReader(response.Body, maxPluginWheelBytes+1))
	if err != nil || len(contents) > maxPluginWheelBytes {
		return nil, fmt.Errorf("read bounded plugin wheel")
	}
	digest := sha256.Sum256(contents)
	if int64(len(contents)) != artifact.Size ||
		hex.EncodeToString(digest[:]) != artifact.Digests.SHA256 {
		return nil, fmt.Errorf("plugin wheel does not match PyPI metadata")
	}
	return contents, nil
}

// validPluginArtifactURL prevents project metadata from selecting another host.
func (a *application) validPluginArtifactURL(value string) (*url.URL, error) {
	location, err := url.Parse(value)
	if err != nil || location.Scheme != "https" || location.User != nil ||
		location.Hostname() == "" || location.RawQuery != "" || location.Fragment != "" {
		return nil, fmt.Errorf("PyPI returned an unsafe plugin wheel URL")
	}
	base, _ := url.Parse(a.system.pypiBaseURL)
	allowed := strings.EqualFold(location.Hostname(), base.Hostname())
	if strings.EqualFold(base.Hostname(), "pypi.org") {
		allowed = strings.EqualFold(location.Hostname(), "files.pythonhosted.org")
	}
	if !allowed {
		return nil, fmt.Errorf("PyPI returned a plugin wheel on an untrusted host")
	}
	return location, nil
}

// inspectPluginWheel binds the package name to one entry point and manifest.
func inspectPluginWheel(contents []byte, projectName, release string) error {
	reader, err := zip.NewReader(bytes.NewReader(contents), int64(len(contents)))
	if err != nil || len(reader.File) > maxPluginWheelFiles {
		return fmt.Errorf("invalid or oversized plugin wheel")
	}
	entrypointFile, err := findPluginEntryPointFile(reader.File)
	if err != nil {
		return err
	}
	entrypointBytes, err := readPluginWheelMetadata(entrypointFile)
	if err != nil {
		return err
	}
	entrypoint, err := parsePluginEntryPointFile(string(entrypointBytes))
	if err != nil {
		return err
	}
	return validatePluginWheelContent(reader.File, entrypoint, projectName, release)
}

// validatePluginWheelContent checks the three resources that define compatibility.
func validatePluginWheelContent(
	files []*zip.File, entrypoint pluginEntryPoint, projectName, release string,
) error {
	slug := extensionProjectSlug(projectName)
	if normalizeProjectName(entrypoint.Name) != slug {
		return fmt.Errorf("plugin entry point name does not match project name")
	}
	root, err := pluginModuleRoot(entrypoint.Value)
	if err != nil {
		return err
	}
	stem, kind := extensionWheelFormat(entrypoint.Value)
	manifestFile, err := findUniqueWheelFile(files, root+"/"+stem+".yaml")
	if err != nil {
		return err
	}
	if _, err := findUniqueWheelFile(files, root+"/"+stem+".py"); err != nil {
		return err
	}
	manifestBytes, err := readPluginWheelMetadata(manifestFile)
	if err != nil {
		return err
	}
	return validatePluginWheelManifest(manifestBytes, entrypoint.Name, release, kind, stem+":"+stem)
}

// findPluginEntryPointFile requires one unambiguous distribution metadata file.
func findPluginEntryPointFile(files []*zip.File) (*zip.File, error) {
	var match *zip.File
	for _, file := range files {
		if strings.HasSuffix(file.Name, ".dist-info/entry_points.txt") {
			if match != nil {
				return nil, fmt.Errorf("plugin wheel has ambiguous metadata")
			}
			match = file
		}
	}
	if match == nil {
		return nil, fmt.Errorf("plugin wheel has no entry points")
	}
	return match, nil
}

// findUniqueWheelFile locates required content without extracting the archive.
func findUniqueWheelFile(files []*zip.File, name string) (*zip.File, error) {
	var match *zip.File
	for _, file := range files {
		if file.Name == name {
			if match != nil {
				return nil, fmt.Errorf("plugin wheel contains duplicate files")
			}
			match = file
		}
	}
	if match == nil {
		return nil, fmt.Errorf("plugin wheel is missing %s", path.Base(name))
	}
	return match, nil
}

// readPluginWheelMetadata bounds decompression of the two inspected resources.
func readPluginWheelMetadata(file *zip.File) ([]byte, error) {
	reader, err := file.Open()
	if err != nil {
		return nil, err
	}
	defer reader.Close()
	contents, err := io.ReadAll(io.LimitReader(reader, maxPluginMetadataEntry+1))
	if err != nil || len(contents) > maxPluginMetadataEntry {
		return nil, fmt.Errorf("plugin wheel metadata exceeds its limit")
	}
	return contents, nil
}

// parsePluginEntryPointFile reads the standard INI group emitted by wheels.
func parsePluginEntryPointFile(contents string) (pluginEntryPoint, error) {
	section := ""
	entries := []pluginEntryPoint{}
	for _, raw := range strings.Split(contents, "\n") {
		line := strings.TrimSpace(raw)
		if ignoredPluginEntryPointLine(line) {
			continue
		}
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			section = strings.TrimSpace(line[1 : len(line)-1])
			continue
		}
		entry, found, err := parsePluginEntryPointLine(section, line)
		if err != nil {
			return pluginEntryPoint{}, err
		}
		if found {
			entries = append(entries, entry)
		}
	}
	if len(entries) != 1 || !validPythonIdentifier(entries[0].Name) {
		return pluginEntryPoint{}, fmt.Errorf("wheel must declare one Harnest plugin")
	}
	return entries[0], nil
}

func ignoredPluginEntryPointLine(line string) bool {
	return line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, ";")
}

// parsePluginEntryPointLine isolates the only INI group search consumes.
func parsePluginEntryPointLine(
	section, line string,
) (pluginEntryPoint, bool, error) {
	if section != pluginEntryPointGroup && section != "harnest.extensions" {
		return pluginEntryPoint{}, false, nil
	}
	name, value, found := strings.Cut(line, "=")
	if !found {
		return pluginEntryPoint{}, false, fmt.Errorf("invalid Harnest entry point")
	}
	stem, _ := extensionWheelFormat(strings.TrimSpace(value))
	if (section == "harnest.extensions") != (stem == "extension") {
		return pluginEntryPoint{}, false, fmt.Errorf("Harnest entry point group does not match its format")
	}
	return pluginEntryPoint{
		Name: strings.TrimSpace(name), Value: strings.TrimSpace(value),
	}, true, nil
}

// pluginModuleRoot binds the standard entry point to the fixed runtime object.
func pluginModuleRoot(value string) (string, error) {
	module, attribute, found := strings.Cut(value, ":")
	if !found || (attribute != "plugin" && attribute != "extension") || strings.ContainsAny(value, " []") {
		return "", fmt.Errorf("Harnest entry point must end in .extension:extension (or legacy .plugin:plugin)")
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

// validatePluginWheelManifest binds runtime identity without full installation checks.
func validatePluginWheelManifest(contents []byte, entryName, release, kind, entrypoint string) error {
	var manifest pluginWheelManifest
	decoder := yaml.NewDecoder(bytes.NewReader(contents))
	decoder.KnownFields(true)
	if err := decoder.Decode(&manifest); err != nil {
		return fmt.Errorf("invalid plugin manifest")
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return fmt.Errorf("plugin manifest must contain one document")
	}
	if manifest.APIVersion != "harnest.dev/v1alpha1" ||
		manifest.Kind != kind || manifest.Runtime.Entrypoint != entrypoint ||
		manifest.Metadata.Name != entryName || manifest.Metadata.Version != release {
		return fmt.Errorf("plugin manifest identity does not match its distribution")
	}
	return nil
}

func safePluginMetadataValue(value string, limit int) bool {
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

// readPluginInspection reuses compatibility decisions for immutable artifacts.
func (a *application) readPluginInspection(
	name, release string, artifact pypiReleaseFile,
) (pluginInspection, bool) {
	path, err := a.pluginInspectionPath(artifact.Digests.SHA256)
	if err != nil {
		return pluginInspection{}, false
	}
	contents, found := readPluginInspectionFile(path)
	if !found {
		return pluginInspection{}, false
	}
	var inspection pluginInspection
	if json.Unmarshal(contents, &inspection) != nil || !validPluginInspection(
		inspection, name, release, artifact.Digests.SHA256,
	) {
		return pluginInspection{}, false
	}
	return inspection, true
}

// readPluginInspectionFile rejects links and oversized cache entries.
func readPluginInspectionFile(path string) ([]byte, bool) {
	info, err := os.Lstat(path)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() ||
		info.Size() > maxPluginInspectionBytes {
		return nil, false
	}
	contents, err := os.ReadFile(path)
	return contents, err == nil
}

// validPluginInspection prevents decisions crossing immutable artifacts.
func validPluginInspection(
	inspection pluginInspection, name, release, digest string,
) bool {
	return inspection.Version == pluginInspectionVersion &&
		normalizeProjectName(inspection.Name) == normalizeProjectName(name) &&
		inspection.Release == release && inspection.SHA256 == digest
}

// pluginInspectionPath keys immutable decisions by artifact digest.
func (a *application) pluginInspectionPath(digest string) (string, error) {
	cacheDirectory := a.system.userCacheDir
	if cacheDirectory == nil {
		cacheDirectory = os.UserCacheDir
	}
	root, err := cacheDirectory()
	if err != nil {
		return "", fmt.Errorf("resolve user cache directory: %w", err)
	}
	return filepath.Join(root, "harnest", "plugins", "inspections", digest+".json"), nil
}

// writePluginInspection best-effort publishes non-executable cache state.
func (a *application) writePluginInspection(inspection pluginInspection) {
	path, err := a.pluginInspectionPath(inspection.SHA256)
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
