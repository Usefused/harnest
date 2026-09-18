package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/mail"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"text/template"
	"unicode/utf8"

	"gopkg.in/yaml.v3"
)

const (
	templateEntryPointGroup    = "harnest.templates"
	templateDistributionPrefix = "harnest-template-"
	maxTemplateWheelBytes      = 64 * 1024 * 1024
	maxTemplateWheelFiles      = 20_000
	maxTemplateResourceBytes   = 64 * 1024 * 1024
	templateManifestFilename   = "harnest-template.yaml"
	templateTreeDirectoryName  = "template"
)

type templateManifest struct {
	APIVersion string `yaml:"apiVersion"`
	Kind       string `yaml:"kind"`
	Metadata   struct {
		Name    string `yaml:"name"`
		Version string `yaml:"version,omitempty"`
	} `yaml:"metadata"`
	Variables []string          `yaml:"variables,omitempty"`
	Services  []templateService `yaml:"services,omitempty"`
}

// templateService declares one inert backing service rendered into docker-compose.yml.
type templateService struct {
	Name        string               `yaml:"name"`
	Image       string               `yaml:"image"`
	Command     []string             `yaml:"command,omitempty"`
	Ports       []string             `yaml:"ports,omitempty"`
	Environment map[string]string    `yaml:"environment,omitempty"`
	Healthcheck *templateHealthcheck `yaml:"healthcheck,omitempty"`
	Volumes     []string             `yaml:"volumes,omitempty"`
	DependsOn   []string             `yaml:"depends_on,omitempty"`
	Provides    map[string]string    `yaml:"provides,omitempty"`
}

type templateHealthcheck struct {
	Test     []string `yaml:"test"`
	Interval string   `yaml:"interval,omitempty"`
	Timeout  string   `yaml:"timeout,omitempty"`
	Retries  int      `yaml:"retries,omitempty"`
}

type templateWheelPackage struct {
	ProjectName string
	Version     string
	Slug        string
	Manifest    templateManifest
	Resources   map[string][]byte
}

type templateData struct {
	Name        string
	AdkName     string
	DisplayName string
}

// templateProjectSlug removes the canonical distribution prefix.
func templateProjectSlug(name string) string {
	return strings.TrimPrefix(normalizeProjectName(name), templateDistributionPrefix)
}

// canonicalTemplateProject accepts a slug while keeping the canonical namespace.
func canonicalTemplateProject(source string) (string, error) {
	if !validPyPIProjectName(source) {
		return "", fmt.Errorf("template must be a harnest-template-* project, a short slug, or an HTTPS wheel URL")
	}
	name := normalizeProjectName(source)
	if !strings.HasPrefix(name, templateDistributionPrefix) {
		name = templateDistributionPrefix + name
	}
	if templateProjectSlug(name) == "" || !validPyPIProjectName(name) {
		return "", fmt.Errorf("invalid Harnest template project %q", source)
	}
	return name, nil
}

// templateWheelFormat derives the entry point singleton from a validated value.
func templateWheelFormat(value string) (string, string) {
	if strings.HasSuffix(value, ".template:template") {
		return "template", "Template"
	}
	return "", ""
}

// parseTemplateEntryPointFile reads the template group emitted by wheels.
func parseTemplateEntryPointFile(contents string) (extensionEntryPoint, error) {
	return parseWheelEntryPoint(contents, templateEntryPointGroup, templateWheelFormat)
}

// templateModuleRoot binds the entry point to the wheel's template data directory.
func templateModuleRoot(value string) (string, error) {
	module, attribute, found := strings.Cut(value, ":")
	if !found || attribute != "template" || strings.ContainsAny(value, " []") {
		return "", fmt.Errorf("Harnest template entry point must end in .template:template")
	}
	parts := strings.Split(module, ".")
	for _, part := range parts {
		if !validPythonIdentifier(part) {
			return "", fmt.Errorf("Harnest template entry point must name a Python module")
		}
	}
	if len(parts) < 2 || parts[len(parts)-1] != attribute {
		return "", fmt.Errorf("Harnest template entry point module must match its singleton name")
	}
	return strings.Join(parts[:len(parts)-1], "/"), nil
}

// createScaffoldFromTemplate downloads and materializes a template wheel without executing it.
func (a *application) createScaffoldFromTemplate(
	ctx context.Context, directory, name, reference, sha256Pin string,
) (string, error) {
	pkg, err := a.downloadTemplatePackage(ctx, reference, sha256Pin)
	if err != nil {
		return "", err
	}
	return materializeTemplate(directory, name, pkg)
}

// downloadTemplatePackage downloads and verifies one template wheel.
func (a *application) downloadTemplatePackage(
	ctx context.Context, reference, sha256Pin string,
) (templateWheelPackage, error) {
	contents, projectName, release, err := a.downloadTemplateReference(ctx, reference, sha256Pin)
	if err != nil {
		return templateWheelPackage{}, err
	}
	pkg, err := readTemplateWheelPackage(contents)
	if err != nil {
		return templateWheelPackage{}, err
	}
	if err := validateTemplateWheelMatch(pkg, projectName, release); err != nil {
		return templateWheelPackage{}, err
	}
	return pkg, nil
}

func validateTemplateWheelMatch(pkg templateWheelPackage, projectName, release string) error {
	if projectName != "" && normalizeProjectName(pkg.ProjectName) != normalizeProjectName(projectName) {
		return fmt.Errorf("template wheel identity does not match the requested project")
	}
	if release != "" && pkg.Version != release {
		return fmt.Errorf("template wheel version does not match the requested release")
	}
	return nil
}

// materializeTemplate writes the inert tree and declared services with rollback on failure.
func materializeTemplate(
	directory, name string, pkg templateWheelPackage,
) (framework string, returnErr error) {
	createdRoot, err := prepareScaffoldDirectory(directory)
	if err != nil {
		return "", err
	}
	created := []string{}
	defer func() {
		if returnErr == nil {
			return
		}
		if createdRoot {
			_ = os.RemoveAll(directory)
			return
		}
		for index := len(created) - 1; index >= 0; index-- {
			_ = os.Remove(created[index])
		}
	}()

	resources, err := renderTemplateResources(pkg.Resources, name)
	if err != nil {
		return "", err
	}
	framework, err = validateTemplateAgent(resources)
	if err != nil {
		return "", err
	}
	if err := applyTemplateServices(resources, name, pkg.Manifest.Services); err != nil {
		return "", err
	}
	if err := writeTemplateTree(directory, resources, &created); err != nil {
		return "", err
	}
	return framework, nil
}

// downloadTemplateReference accepts an HTTPS wheel URL or a PyPI project/slug.
func (a *application) downloadTemplateReference(
	ctx context.Context, reference, sha256Pin string,
) (contents []byte, projectName, release string, err error) {
	if location, urlErr := validTemplateArtifactURL(reference); urlErr == nil {
		contents, err = a.downloadTemplateWheel(ctx, location, sha256Pin)
		return contents, "", "", err
	}
	projectName, err = canonicalTemplateProject(reference)
	if err != nil {
		return nil, "", "", err
	}
	metadata, err := a.fetchPyPIExtensionMetadata(ctx, projectName, false)
	if err != nil {
		return nil, "", "", fmt.Errorf("resolve Harnest template %q: %w", projectName, err)
	}
	artifact, err := selectTemplateWheel(metadata.URLs)
	if err != nil {
		return nil, "", "", err
	}
	contents, err = a.downloadExtensionWheel(ctx, artifact)
	if err != nil {
		return nil, "", "", err
	}
	return contents, projectName, metadata.Info.Version, nil
}

// validTemplateArtifactURL permits direct HTTPS downloads on any host.
func validTemplateArtifactURL(value string) (*url.URL, error) {
	location, err := url.Parse(value)
	if err != nil || location.Scheme != "https" || location.User != nil ||
		location.Hostname() == "" || location.RawQuery != "" || location.Fragment != "" {
		return nil, fmt.Errorf("template URL must be HTTPS without credentials, query, or fragment")
	}
	return location, nil
}

// downloadTemplateWheel bounds the transfer and applies an optional pinned digest.
func (a *application) downloadTemplateWheel(
	ctx context.Context, location *url.URL, sha256Pin string,
) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, location.String(), nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("User-Agent", "harnest/"+a.version)
	response, err := a.extensionHTTPClient().Do(request)
	if err != nil {
		return nil, fmt.Errorf("download template: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || response.ContentLength > maxTemplateWheelBytes {
		return nil, fmt.Errorf("download template: HTTP %d", response.StatusCode)
	}
	contents, err := io.ReadAll(io.LimitReader(response.Body, maxTemplateWheelBytes+1))
	if err != nil || len(contents) > maxTemplateWheelBytes {
		return nil, fmt.Errorf("read bounded template wheel")
	}
	if sha256Pin != "" {
		digest := sha256.Sum256(contents)
		if !validSHA256(sha256Pin) || hex.EncodeToString(digest[:]) != strings.ToLower(sha256Pin) {
			return nil, fmt.Errorf("template wheel does not match --template-sha256")
		}
	}
	return contents, nil
}

// selectTemplateWheel chooses the smallest bounded universal template wheel.
func selectTemplateWheel(files []pypiReleaseFile) (pypiReleaseFile, error) {
	wheels := make([]pypiReleaseFile, 0, len(files))
	for _, file := range files {
		if file.PackageType == "bdist_wheel" && !file.Yanked &&
			validUniversalWheelFilename(file.Filename) && file.Size > 0 &&
			file.Size <= maxTemplateWheelBytes && validSHA256(file.Digests.SHA256) {
			wheels = append(wheels, file)
		}
	}
	if len(wheels) == 0 {
		return pypiReleaseFile{}, fmt.Errorf("latest release has no universal py3-none-any template wheel")
	}
	sort.Slice(wheels, func(i, j int) bool {
		if wheels[i].Size != wheels[j].Size {
			return wheels[i].Size < wheels[j].Size
		}
		return wheels[i].Filename < wheels[j].Filename
	})
	return wheels[0], nil
}

// templateWheelIdentity binds distribution and entry-point identity for one wheel.
type templateWheelIdentity struct {
	ProjectName string
	Version     string
	Slug        string
	Root        string
}

// readTemplateWheelPackage verifies wheel identity and extracts the inert template tree.
func readTemplateWheelPackage(contents []byte) (templateWheelPackage, error) {
	reader, err := templateWheelReader(contents)
	if err != nil {
		return templateWheelPackage{}, err
	}
	identity, err := readTemplateWheelIdentity(reader.File)
	if err != nil {
		return templateWheelPackage{}, err
	}
	manifest, resources, err := readTemplateWheelContent(reader.File, identity)
	if err != nil {
		return templateWheelPackage{}, err
	}
	return templateWheelPackage{
		ProjectName: identity.ProjectName, Version: identity.Version, Slug: identity.Slug,
		Manifest: manifest, Resources: resources,
	}, nil
}

func templateWheelReader(contents []byte) (*zip.Reader, error) {
	reader, err := zip.NewReader(bytes.NewReader(contents), int64(len(contents)))
	if err != nil || len(reader.File) > maxTemplateWheelFiles {
		return nil, fmt.Errorf("invalid or oversized template wheel")
	}
	return reader, nil
}

// readTemplateWheelIdentity derives distribution and entry-point identity from METADATA.
func readTemplateWheelIdentity(files []*zip.File) (templateWheelIdentity, error) {
	entrypoint, err := readTemplateEntryPoint(files)
	if err != nil {
		return templateWheelIdentity{}, err
	}
	root, err := templateModuleRoot(entrypoint.Value)
	if err != nil {
		return templateWheelIdentity{}, err
	}
	projectName, version, err := readTemplateWheelMetadataIdentity(files)
	if err != nil {
		return templateWheelIdentity{}, err
	}
	slug := templateProjectSlug(projectName)
	if slug == "" || normalizeProjectName(entrypoint.Name) != slug {
		return templateWheelIdentity{}, fmt.Errorf("template entry point name does not match its distribution")
	}
	return templateWheelIdentity{ProjectName: projectName, Version: version, Slug: slug, Root: root}, nil
}

func readTemplateEntryPoint(files []*zip.File) (extensionEntryPoint, error) {
	entrypointFile, err := findExtensionEntryPointFile(files)
	if err != nil {
		return extensionEntryPoint{}, fmt.Errorf("template wheel: %w", err)
	}
	entrypointBytes, err := readExtensionWheelMetadata(entrypointFile)
	if err != nil {
		return extensionEntryPoint{}, fmt.Errorf("template wheel: %w", err)
	}
	return parseTemplateEntryPointFile(string(entrypointBytes))
}

func readTemplateWheelContent(
	files []*zip.File, identity templateWheelIdentity,
) (templateManifest, map[string][]byte, error) {
	manifest, err := readTemplateWheelManifest(files, identity)
	if err != nil {
		return templateManifest{}, nil, err
	}
	resources, err := readTemplateWheelResources(files, identity.Root)
	if err != nil {
		return templateManifest{}, nil, err
	}
	return manifest, resources, nil
}

func readTemplateWheelManifest(
	files []*zip.File, identity templateWheelIdentity,
) (templateManifest, error) {
	manifestFile, err := findUniqueWheelFile(files, identity.Root+"/"+templateManifestFilename)
	if err != nil {
		return templateManifest{}, fmt.Errorf("template wheel: %w", err)
	}
	manifestBytes, err := readExtensionWheelMetadata(manifestFile)
	if err != nil {
		return templateManifest{}, fmt.Errorf("template wheel: %w", err)
	}
	manifest, err := decodeTemplateManifest(manifestBytes)
	if err != nil {
		return templateManifest{}, err
	}
	if manifest.Metadata.Name != identity.Slug {
		return templateManifest{}, fmt.Errorf("template manifest name does not match its distribution")
	}
	return manifest, nil
}

// readTemplateWheelMetadataIdentity derives distribution identity from wheel METADATA.
func readTemplateWheelMetadataIdentity(files []*zip.File) (string, string, error) {
	metadataFile, err := findExtensionWheelMetadata(files)
	if err != nil {
		return "", "", err
	}
	contents, err := readExtensionWheelMetadata(metadataFile)
	if err != nil {
		return "", "", err
	}
	message, err := mail.ReadMessage(bytes.NewReader(contents))
	if err != nil {
		return "", "", fmt.Errorf("invalid template wheel METADATA")
	}
	name := message.Header.Get("Name")
	version := message.Header.Get("Version")
	if !validPyPIProjectName(name) || !safeExtensionMetadataValue(version, 50) {
		return "", "", fmt.Errorf("template wheel METADATA identity is invalid")
	}
	return name, version, nil
}

// decodeTemplateManifest validates the inert authoring contract without importing code.
func decodeTemplateManifest(contents []byte) (templateManifest, error) {
	var manifest templateManifest
	if err := yaml.Unmarshal(contents, &manifest); err != nil {
		return templateManifest{}, fmt.Errorf("invalid %s: %w", templateManifestFilename, err)
	}
	if manifest.APIVersion != "harnest.dev/v1alpha1" || manifest.Kind != "Template" {
		return templateManifest{}, fmt.Errorf("%s must declare apiVersion harnest.dev/v1alpha1 and kind: Template", templateManifestFilename)
	}
	if !scaffoldNamePattern.MatchString(manifest.Metadata.Name) {
		return templateManifest{}, fmt.Errorf("%s metadata.name must be a kebab-case slug", templateManifestFilename)
	}
	for _, variable := range manifest.Variables {
		switch variable {
		case "name", "adkName", "displayName":
		default:
			return templateManifest{}, fmt.Errorf("%s declares unknown variable %q", templateManifestFilename, variable)
		}
	}
	if err := validateTemplateServices(manifest.Services); err != nil {
		return templateManifest{}, err
	}
	return manifest, nil
}

// readTemplateWheelResources extracts only the inert tree below <root>/template/.
func readTemplateWheelResources(
	files []*zip.File, root string,
) (map[string][]byte, error) {
	resources := map[string][]byte{}
	casefoldPaths := map[string]string{}
	prefix := root + "/" + templateTreeDirectoryName + "/"
	remaining := int64(maxTemplateResourceBytes)
	for _, file := range files {
		if !strings.HasPrefix(file.Name, prefix) || strings.HasSuffix(file.Name, "/") {
			continue
		}
		relative := strings.TrimPrefix(file.Name, prefix)
		if relative == "" {
			continue
		}
		if err := validateTemplateWheelResource(relative, file); err != nil {
			return nil, err
		}
		casefold := strings.ToLower(relative)
		if previous, duplicate := casefoldPaths[casefold]; duplicate {
			return nil, fmt.Errorf("template wheel resources conflict by case: %s and %s", previous, relative)
		}
		casefoldPaths[casefold] = relative
		if _, duplicate := resources[relative]; duplicate {
			return nil, fmt.Errorf("template wheel contains duplicate resource %s", relative)
		}
		contents, err := readBoundedTemplateFile(file, &remaining)
		if err != nil {
			return nil, err
		}
		resources[relative] = contents
	}
	if len(resources) == 0 {
		return nil, fmt.Errorf("template wheel contains no files under %s", prefix)
	}
	return resources, nil
}

// validateTemplateWheelResource confines one archive entry before extraction.
func validateTemplateWheelResource(relative string, file *zip.File) error {
	if !safeExtensionWheelResourcePath(relative) {
		return fmt.Errorf("template wheel contains an unsafe resource path")
	}
	if file.Mode()&os.ModeSymlink != 0 || !file.Mode().IsRegular() {
		return fmt.Errorf("template resources must be regular files: %s", relative)
	}
	if file.UncompressedSize64 > uint64(maxTemplateResourceBytes) {
		return fmt.Errorf("template resource exceeds the installed-size limit: %s", relative)
	}
	return nil
}

// readBoundedTemplateFile prevents compressed entries from expanding past the tree cap.
func readBoundedTemplateFile(file *zip.File, remaining *int64) ([]byte, error) {
	if *remaining <= 0 || file.UncompressedSize64 > uint64(*remaining) {
		return nil, fmt.Errorf("template wheel exceeds the installed-size limit")
	}
	reader, err := file.Open()
	if err != nil {
		return nil, fmt.Errorf("read template resource: %w", err)
	}
	defer reader.Close()
	contents, err := io.ReadAll(io.LimitReader(reader, *remaining+1))
	if err != nil || int64(len(contents)) > *remaining {
		return nil, fmt.Errorf("read bounded template resource")
	}
	*remaining -= int64(len(contents))
	return contents, nil
}

// renderTemplateResources substitutes only the allowlisted scaffold data set.
func renderTemplateResources(resources map[string][]byte, name string) (map[string][]byte, error) {
	data := templateData{
		Name: name, AdkName: adkName(name), DisplayName: displayName(name),
	}
	rendered := make(map[string][]byte, len(resources))
	for relative, contents := range resources {
		if !isTemplateText(contents) {
			rendered[relative] = contents
			continue
		}
		value, err := renderTemplateText(relative, contents, data)
		if err != nil {
			return nil, err
		}
		rendered[relative] = value
	}
	return rendered, nil
}

// isTemplateText leaves binary resources untouched while permitting placeholder substitution.
func isTemplateText(contents []byte) bool {
	return utf8.Valid(contents) && bytes.IndexByte(contents, 0) < 0
}

// renderTemplateText runs data-only text/template; no shell or filesystem access is exposed.
func renderTemplateText(relative string, contents []byte, data templateData) ([]byte, error) {
	tmpl, err := template.New(relative).Option("missingkey=error").Parse(string(contents))
	if err != nil {
		return nil, fmt.Errorf("parse template file %s: %w", relative, err)
	}
	var buffer bytes.Buffer
	if err := tmpl.Execute(&buffer, data); err != nil {
		return nil, fmt.Errorf("render template file %s: %w", relative, err)
	}
	return buffer.Bytes(), nil
}

// validateTemplateAgent confirms the tree is a Harnest agent without importing it.
func validateTemplateAgent(resources map[string][]byte) (string, error) {
	config, ok := resources["config.yaml"]
	if !ok {
		return "", fmt.Errorf("template is missing config.yaml")
	}
	if _, ok := resources["agent.py"]; !ok {
		return "", fmt.Errorf("template is missing agent.py")
	}
	var document struct {
		Kind string `yaml:"kind"`
		Spec struct {
			Framework struct {
				Name string `yaml:"name"`
			} `yaml:"framework"`
			Runtime struct {
				DependencyFile string `yaml:"dependencyFile"`
			} `yaml:"runtime"`
		} `yaml:"spec"`
	}
	if err := yaml.Unmarshal(config, &document); err != nil {
		return "", fmt.Errorf("invalid template config.yaml: %w", err)
	}
	if document.Kind != "Agent" {
		return "", fmt.Errorf("template config.yaml must declare kind: Agent")
	}
	framework := document.Spec.Framework.Name
	if framework != "adk" && framework != "langgraph" {
		return "", fmt.Errorf("template config.yaml must select an adk or langgraph framework")
	}
	if dependency := document.Spec.Runtime.DependencyFile; dependency != "" {
		if _, ok := resources[dependency]; !ok {
			return "", fmt.Errorf("template config.yaml references missing dependency file %s", dependency)
		}
	}
	return framework, nil
}

// writeTemplateTree creates template files with the same rollback policy as built-in scaffolds.
func writeTemplateTree(root string, files map[string][]byte, created *[]string) error {
	paths := make([]string, 0, len(files))
	for relative := range files {
		paths = append(paths, relative)
	}
	sort.Strings(paths)
	for _, relative := range paths {
		path := filepath.Join(root, filepath.FromSlash(relative))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return fmt.Errorf("create template directory: %w", err)
		}
		if err := createScaffoldFile(path, string(files[relative])); err != nil {
			return err
		}
		*created = append(*created, path)
	}
	return nil
}
