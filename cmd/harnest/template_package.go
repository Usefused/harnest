package main

import (
	"archive/zip"
	"bytes"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"
)

// newTemplateCommand owns authoring and packaging of reusable Harnest templates.
func (a *application) newTemplateCommand() *cobra.Command {
	command := &cobra.Command{
		Use:   "template",
		Short: "Package an agent as a reusable Harnest template",
		Long: `Package a Harnest agent as a universal harnest-template-* wheel that
harnest init --template downloads and materializes. Packaging copies the agent
tree verbatim into the wheel's template/ directory; it never imports or executes
the agent code.`,
	}
	command.AddCommand(a.newTemplatePackageCommand())
	return command
}

// newTemplatePackageCommand packages one validated agent directory as a template wheel.
func (a *application) newTemplatePackageCommand() *cobra.Command {
	var output string
	var slug string
	var version string
	command := &cobra.Command{
		Use:   "package [AGENT-DIR]",
		Short: "Package an agent directory into a template wheel",
		Args:  cobra.MaximumNArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			source := "."
			if len(arguments) == 1 {
				source = arguments[0]
			}
			result, err := a.packageTemplate(source, slug, version, output)
			if err != nil {
				return err
			}
			fmt.Fprintf(
				command.OutOrStdout(),
				"Packaged template %s as %s\n",
				result.Slug,
				result.Path,
			)
			return nil
		},
	}
	command.Flags().StringVarP(
		&output, "output", "o", "",
		"wheel output path (default dist/<module>-<version>-py3-none-any.whl)",
	)
	command.Flags().StringVar(
		&slug, "slug", "",
		"template slug override (default derives from config.yaml metadata.name)",
	)
	command.Flags().StringVar(
		&version, "version", "",
		"template version (defaults to 0.1.0 or the authored manifest version)",
	)
	return command
}

type templatePackageResult struct {
	Slug string
	Path string
}

// packageTemplate validates the agent, collects its tree, and emits a template wheel.
func (a *application) packageTemplate(
	source, slug, version, output string,
) (templatePackageResult, error) {
	root, authored, err := loadTemplateSource(source)
	if err != nil {
		return templatePackageResult{}, err
	}
	slug, version, err = resolveTemplateIdentity(root, slug, version, authored)
	if err != nil {
		return templatePackageResult{}, err
	}
	module := templateModuleName(slug)
	outputPath := output
	if outputPath == "" {
		outputPath = filepath.Join(root, "dist", fmt.Sprintf("%s-%s-py3-none-any.whl", module, version))
	}
	files, err := collectTemplateTree(root, outputPath)
	if err != nil {
		return templatePackageResult{}, err
	}
	if _, err := validateTemplateAgent(files); err != nil {
		return templatePackageResult{}, err
	}
	if err := a.publishTemplateWheel(files, slug, module, version, outputPath, templateServicesOf(authored)); err != nil {
		return templatePackageResult{}, err
	}
	return templatePackageResult{Slug: slug, Path: outputPath}, nil
}

// loadTemplateSource resolves the agent root and any authored template manifest.
func loadTemplateSource(source string) (string, *templateManifest, error) {
	root, err := validatedAgentProject(source)
	if err != nil {
		return "", nil, err
	}
	authored, err := readAuthoredTemplateManifest(root)
	if err != nil {
		return "", nil, err
	}
	return root, authored, nil
}

// readAuthoredTemplateManifest loads an optional root harnest-template.yaml.
func readAuthoredTemplateManifest(root string) (*templateManifest, error) {
	contents, err := os.ReadFile(filepath.Join(root, templateManifestFilename))
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", templateManifestFilename, err)
	}
	manifest, err := decodeTemplateManifest(contents)
	if err != nil {
		return nil, err
	}
	return &manifest, nil
}

// resolveTemplateIdentity merges CLI overrides with the authored manifest and config.yaml.
func resolveTemplateIdentity(
	root, slug, version string, authored *templateManifest,
) (string, string, error) {
	if version == "" {
		version = "0.1.0"
		if authored != nil && authored.Metadata.Version != "" {
			version = authored.Metadata.Version
		}
	}
	if !validTemplateVersion(version) {
		return "", "", fmt.Errorf("--version must use only letters, digits, dots, and underscores")
	}
	slug, err := resolveTemplateSlug(root, slug, authored)
	if err != nil {
		return "", "", err
	}
	return slug, version, nil
}

// resolveTemplateSlug derives a kebab-case slug from CLI, manifest, then config.yaml.
func resolveTemplateSlug(root, override string, authored *templateManifest) (string, error) {
	if override != "" {
		return validateTemplateSlug(override)
	}
	if authored != nil && authored.Metadata.Name != "" {
		return validateTemplateSlug(authored.Metadata.Name)
	}
	return slugFromAgentConfig(root)
}

func validateTemplateSlug(slug string) (string, error) {
	if !scaffoldNamePattern.MatchString(slug) {
		return "", fmt.Errorf("template slug must be a kebab-case name beginning with a letter")
	}
	return slug, nil
}

func slugFromAgentConfig(root string) (string, error) {
	config, err := os.ReadFile(filepath.Join(root, "config.yaml"))
	if err != nil {
		return "", fmt.Errorf("read config.yaml: %w", err)
	}
	var document struct {
		Metadata struct {
			Name string `yaml:"name"`
		} `yaml:"metadata"`
	}
	if err := yaml.Unmarshal(config, &document); err != nil {
		return "", fmt.Errorf("invalid config.yaml: %w", err)
	}
	return validateTemplateSlug(document.Metadata.Name)
}

func templateServicesOf(authored *templateManifest) []templateService {
	if authored == nil {
		return nil
	}
	return authored.Services
}

// publishTemplateWheel assembles and atomically writes the wheel.
func (a *application) publishTemplateWheel(
	files map[string][]byte, slug, module, version, outputPath string, services []templateService,
) error {
	entries, err := templateWheelEntries(files, slug, module, version, a.version, services)
	if err != nil {
		return err
	}
	archive, err := buildTemplateWheelArchive(entries)
	if err != nil {
		return err
	}
	return writeTemplateWheelOutput(archive, outputPath)
}

// templateModuleName maps a kebab-case slug to the wheel's importable module directory.
func templateModuleName(slug string) string {
	return "harnest_template_" + strings.ReplaceAll(slug, "-", "_")
}

// validTemplateVersion keeps wheel filename and METADATA components well-formed.
func validTemplateVersion(value string) bool {
	if value == "" || len(value) > 50 || !safeExtensionMetadataValue(value, 50) {
		return false
	}
	if value == "." || strings.HasPrefix(value, ".") || strings.HasSuffix(value, ".") {
		return false
	}
	for index := 0; index < len(value); index++ {
		if !templateVersionCharacter(value[index]) {
			return false
		}
	}
	return true
}

func templateVersionCharacter(character byte) bool {
	return asciiAlphaNumeric(character) || character == '.' || character == '_'
}

// templateTreeCollector owns one bounded walk of the agent source tree.
type templateTreeCollector struct {
	root           string
	outputRelative string
	outputInside   bool
	files          map[string][]byte
	total          int64
}

// collectTemplateTree copies the agent tree while excluding build and cache state.
func collectTemplateTree(root, outputPath string) (map[string][]byte, error) {
	outputAbsolute, err := filepath.Abs(outputPath)
	if err != nil {
		return nil, err
	}
	outputRelative, outputRelErr := filepath.Rel(root, outputAbsolute)
	collector := templateTreeCollector{
		root:           root,
		outputRelative: filepath.ToSlash(outputRelative),
		outputInside:   outputRelErr == nil,
		files:          map[string][]byte{},
	}
	if err := filepath.WalkDir(root, collector.visit); err != nil {
		return nil, err
	}
	if len(collector.files) == 0 {
		return nil, fmt.Errorf("agent directory contains no files to package")
	}
	return collector.files, nil
}

func (c *templateTreeCollector) visit(path string, entry fs.DirEntry, walkErr error) error {
	if walkErr != nil {
		return walkErr
	}
	if path == c.root {
		return nil
	}
	relative, err := filepath.Rel(c.root, path)
	if err != nil {
		return err
	}
	if entry.IsDir() {
		if templateExcludedDirectory(entry.Name()) {
			return filepath.SkipDir
		}
		return nil
	}
	relative = filepath.ToSlash(relative)
	if relative == templateManifestFilename {
		return nil
	}
	if c.outputInside && relative == c.outputRelative {
		return nil
	}
	return c.collectFile(path, relative)
}

func (c *templateTreeCollector) collectFile(path, relative string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("template tree cannot contain symlinks: %s", relative)
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("template tree must contain only regular files: %s", relative)
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	c.total += int64(len(contents))
	if c.total > maxTemplateResourceBytes {
		return fmt.Errorf("template tree exceeds the installed-size limit")
	}
	c.files[relative] = contents
	return nil
}

// templateExcludedDirectory omits build, environment, and source-control state.
func templateExcludedDirectory(name string) bool {
	switch name {
	case ".harnest", ".venv", "__pycache__", ".pytest_cache", ".git", ".cache", "dist", "build", "node_modules":
		return true
	}
	return false
}

// templateWheelEntries assembles every wheel member from the collected tree.
func templateWheelEntries(
	files map[string][]byte, slug, module, version, generatorVersion string,
	services []templateService,
) (map[string][]byte, error) {
	entries := map[string][]byte{}
	manifest, err := templateManifestSource(slug, version, services)
	if err != nil {
		return nil, err
	}
	entries[module+"/"+templateManifestFilename] = manifest
	for relative, contents := range files {
		entries[module+"/template/"+relative] = contents
	}
	distInfo := module + "-" + version + ".dist-info"
	entryName := strings.ReplaceAll(slug, "-", "_")
	entries[distInfo+"/METADATA"] = []byte(templateMetadataSource(slug, version))
	entries[distInfo+"/WHEEL"] = fmt.Appendf(nil,
		"Wheel-Version: 1.0\nGenerator: harnest/%s\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
		generatorVersion,
	)
	entries[distInfo+"/entry_points.txt"] = fmt.Appendf(nil,
		"[harnest.templates]\n%s = %s.template:template\n", entryName, module,
	)
	return entries, nil
}

func templateManifestSource(slug, version string, services []templateService) ([]byte, error) {
	manifest := templateManifest{
		APIVersion: "harnest.dev/v1alpha1",
		Kind:       "Template",
		Variables:  []string{"name", "adkName", "displayName"},
		Services:   services,
	}
	manifest.Metadata.Name = slug
	manifest.Metadata.Version = version
	return yaml.Marshal(manifest)
}

func templateMetadataSource(slug, version string) string {
	return fmt.Sprintf(
		"Metadata-Version: 2.1\nName: harnest-template-%s\nVersion: %s\n",
		slug, version,
	)
}

// buildTemplateWheelArchive writes deterministic, regular-file-only zip entries.
func buildTemplateWheelArchive(entries map[string][]byte) ([]byte, error) {
	var buffer bytes.Buffer
	writer := zip.NewWriter(&buffer)
	names := make([]string, 0, len(entries))
	for name := range entries {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		header := &zip.FileHeader{Name: name, Method: zip.Deflate}
		header.SetMode(0o644)
		file, err := writer.CreateHeader(header)
		if err != nil {
			return nil, err
		}
		if _, err := file.Write(entries[name]); err != nil {
			return nil, err
		}
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}

// writeTemplateWheelOutput stages the wheel and renames it into place atomically.
func writeTemplateWheelOutput(contents []byte, path string) error {
	parent := filepath.Dir(path)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return fmt.Errorf("create template output directory: %w", err)
	}
	staged, err := os.CreateTemp(parent, ".harnest-template-*.whl")
	if err != nil {
		return fmt.Errorf("stage template wheel: %w", err)
	}
	stagedPath := staged.Name()
	defer func() {
		_ = staged.Close()
		_ = os.Remove(stagedPath)
	}()
	if _, err := staged.Write(contents); err != nil {
		return fmt.Errorf("write template wheel: %w", err)
	}
	if err := staged.Close(); err != nil {
		return fmt.Errorf("close template wheel: %w", err)
	}
	if err := os.Rename(stagedPath, path); err != nil {
		return fmt.Errorf("publish template wheel: %w", err)
	}
	return nil
}
