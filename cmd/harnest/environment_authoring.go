// Managed IDE/authoring Python environments for pack (pack.yaml) and standalone extension (extension.yaml) folders.

package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"

	"harnest.dev/harnest/internal/frameworks"
	"harnest.dev/harnest/internal/runtimewheel"
	"harnest.dev/harnest/internal/uvbootstrap"
)

type authoringEnvironmentKind string

const (
	authoringTargetAgent     authoringEnvironmentKind = "agent"
	authoringTargetPack      authoringEnvironmentKind = "pack"
	authoringTargetExtension authoringEnvironmentKind = "extension"

	authoringEnvironmentSlot  = "authoring"
	authoringEnvironmentState = "authoring-environment.json"
	maxAuthoringManifestBytes = 1 << 20
)

type authoringPackManifest struct {
	Packages   []string `yaml:"packages"`
	Extensions []string `yaml:"extensions"`
}

type authoringPackageSpec struct {
	requirement string
	localPath   string
	editable    bool
}

type authoringDependencyPlan struct {
	kind           authoringEnvironmentKind
	root           string
	extras         []string
	packages       []authoringPackageSpec
	requirements   []string
	manifestFiles  []string
	wheelFiles     []string
	localProjFiles []string
}

// detectEnvironmentTarget identifies whether a directory is an agent, pack, or standalone extension root.
func detectEnvironmentTarget(directory string) (authoringEnvironmentKind, string, error) {
	resolved, err := filepath.Abs(directory)
	// Return resolution failures early so callers see the invalid target path directly.
	if err != nil {
		return "", "", fmt.Errorf("resolve project directory: %w", err)
	}
	// Resolve symlinks when the directory exists so relative IDE and VS Code paths match canonical disk roots.
	if canonical, symErr := filepath.EvalSymlinks(resolved); symErr == nil {
		resolved = canonical
	}
	// Prefer agent config.yaml whenever present so existing agent projects keep their locked profile behavior.
	if regularFileExists(filepath.Join(resolved, "config.yaml")) {
		return authoringTargetAgent, resolved, nil
	}
	// Detect pack roots next because packs may contain nested extensions under extensions/.
	if regularFileExists(filepath.Join(resolved, "pack.yaml")) {
		return authoringTargetPack, resolved, nil
	}
	// Fall back to standalone extension roots when extension.yaml is at the target directory.
	if regularFileExists(filepath.Join(resolved, "extension.yaml")) {
		return authoringTargetExtension, resolved, nil
	}
	// Default to agent so loadAgentBundle emits the established missing-config diagnostic.
	return authoringTargetAgent, resolved, nil
}

// regularFileExists reports whether path exists as a non-symlink regular file.
func regularFileExists(path string) bool {
	info, err := os.Lstat(path)
	return err == nil && info.Mode().IsRegular()
}

// resolveAuthoringFrameworkExtras translates --framework into the Harnest wheel extras for IDE resolution.
func resolveAuthoringFrameworkExtras(selection string) ([]string, error) {
	trimmed := strings.TrimSpace(selection)
	// Default to every registered framework so pack and extension authors get full editor symbol resolution.
	if trimmed == "" || trimmed == "all" {
		all := frameworks.All()
		extras := make([]string, 0, len(all)*2+1)
		for _, framework := range all {
			extras = append(extras, framework.InstallExtra, framework.InstallExtra+"-mcp")
		}
		return append(extras, "test"), nil
	}
	selected, ok := frameworks.Lookup(trimmed)
	// Reject unknown framework flags upfront before creating or mutating any local environment.
	if !ok {
		return nil, fmt.Errorf("--framework must be all, %s", frameworks.Choices())
	}
	return []string{selected.InstallExtra, selected.InstallExtra + "-mcp", "test"}, nil
}

// syncAuthoringEnvironment builds or reuses a managed dev environment for a pack or standalone extension.
func (a *application) syncAuthoringEnvironment(
	command *cobra.Command,
	kind authoringEnvironmentKind,
	root string,
	frameworkFlag string,
) (pythonSelection, error) {
	extras, err := resolveAuthoringFrameworkExtras(frameworkFlag)
	// Validate the requested framework extras before reading manifests or touching disk.
	if err != nil {
		return pythonSelection{}, err
	}
	plan, err := inspectAuthoringDependencyPlan(kind, root, extras)
	// Reject malformed manifests or compiler-owned package declarations before staging uv or wheels.
	if err != nil {
		return pythonSelection{}, err
	}
	wheel, err := a.system.embeddedWheel(a.version)
	// Require the bundled Harnest wheel so authoring environments mirror the active CLI version.
	if err != nil {
		return pythonSelection{}, fmt.Errorf("load embedded Harnest wheel: %w", err)
	}
	paths, err := inspectAuthoringEnvironmentPaths(root)
	// Reject symlinked or unwritable .harnest directories before acquiring the environment lock.
	if err != nil {
		return pythonSelection{}, err
	}
	fingerprint, err := authoringEnvironmentFingerprint(plan, wheel)
	// Abort if any declared dependency input cannot be hashed deterministically.
	if err != nil {
		return pythonSelection{}, err
	}
	unlock, err := lockEnvironment(paths.lock)
	// Serialize concurrent syncs on the same pack or extension workspace.
	if err != nil {
		return pythonSelection{}, err
	}
	defer unlock()
	// Reuse the existing environment when inputs match and Harnest is still importable inside it.
	if selection, ok, probeErr := a.reuseCachedAuthoringEnvironment(command, paths, fingerprint, kind); probeErr != nil {
		return pythonSelection{}, probeErr
	} else if ok {
		return selection, nil
	}
	return a.installAuthoringEnvironment(command, plan, wheel, paths, fingerprint)
}

// reuseCachedAuthoringEnvironment returns the cached authoring interpreter when its fingerprint and CLI probe pass.
func (a *application) reuseCachedAuthoringEnvironment(
	command *cobra.Command,
	paths environmentPaths,
	fingerprint string,
	kind authoringEnvironmentKind,
) (pythonSelection, bool, error) {
	selection, found := cachedAgentPython(paths, fingerprint)
	// Trigger a clean install whenever no matching cached state exists.
	if !found {
		return pythonSelection{}, false, nil
	}
	available, err := a.agentCLIAvailable(command, selection.Executable)
	if err != nil {
		return pythonSelection{}, false, err
	}
	// Rebuild if an external tool cleared Harnest out of the cached authoring environment.
	if !available {
		return pythonSelection{}, false, nil
	}
	selection.Source = string(kind) + " environment"
	return selection, true, nil
}

// inspectAuthoringEnvironmentPaths prepares .harnest/environments and returns state/lock paths.
func inspectAuthoringEnvironmentPaths(root string) (environmentPaths, error) {
	stateRoot := filepath.Join(root, ".harnest")
	// Guard against symlinks in .harnest before creating nested environment folders.
	if err := ensureRegularEnvironmentDirectory(stateRoot); err != nil {
		return environmentPaths{}, err
	}
	// Keep authoring environments under .harnest/environments so syncIDEEnvironmentLink accepts them directly.
	if err := ensureRegularEnvironmentDirectory(filepath.Join(stateRoot, "environments")); err != nil {
		return environmentPaths{}, err
	}
	return environmentPaths{
		root:  stateRoot,
		state: filepath.Join(stateRoot, authoringEnvironmentState),
		lock:  filepath.Join(stateRoot, "environment.lock"),
	}, nil
}

// inspectAuthoringDependencyPlan collects pack packages and extension dependencies with policy checks.
func inspectAuthoringDependencyPlan(
	kind authoringEnvironmentKind, root string, extras []string,
) (authoringDependencyPlan, error) {
	plan := authoringDependencyPlan{
		kind:   kind,
		root:   root,
		extras: extras,
	}
	owned := allCompilerOwnedDistributions()
	// Branch by project kind because packs aggregate pack.yaml plus nested extensions while standalone extensions own one root.
	switch kind {
	case authoringTargetPack:
		if err := collectPackAuthoringInputs(&plan, root, owned); err != nil {
			return authoringDependencyPlan{}, err
		}
	case authoringTargetExtension:
		if err := collectExtensionAuthoringInputs(&plan, root, owned); err != nil {
			return authoringDependencyPlan{}, err
		}
	default:
		return authoringDependencyPlan{}, fmt.Errorf("unsupported authoring environment target: %s", kind)
	}
	sort.Strings(plan.manifestFiles)
	sort.Strings(plan.wheelFiles)
	sort.Strings(plan.localProjFiles)
	return plan, nil
}

// collectPackAuthoringInputs parses pack.yaml packages and discovers both declared and local extensions.
func collectPackAuthoringInputs(
	plan *authoringDependencyPlan, root string, owned map[string]struct{},
) error {
	manifestPath := filepath.Join(root, "pack.yaml")
	contents, err := readRegularExtensionFile(manifestPath, maxAuthoringManifestBytes, "pack manifest")
	// Reject missing, symlinked, or oversized pack manifests before YAML decoding.
	if err != nil {
		return err
	}
	var manifest authoringPackManifest
	// Fail clearly on malformed pack.yaml before inspecting packages or extensions.
	if err := yaml.Unmarshal(contents, &manifest); err != nil {
		return fmt.Errorf("parse pack.yaml: %w", err)
	}
	plan.manifestFiles = append(plan.manifestFiles, manifestPath)
	for _, entry := range manifest.Packages {
		spec, err := parseAuthoringPackPackage(root, entry, owned)
		if err != nil {
			return err
		}
		plan.packages = append(plan.packages, spec)
		recordLocalPackPackageInput(plan, spec)
	}
	extensionDirs, err := discoverPackExtensionDirectories(root, manifest.Extensions)
	// Propagate invalid extension paths declared in pack.yaml rather than silently skipping them.
	if err != nil {
		return err
	}
	for _, extDir := range extensionDirs {
		if err := collectExtensionAuthoringInputs(plan, extDir, owned); err != nil {
			return err
		}
	}
	return nil
}

// recordLocalPackPackageInput tracks local wheel files or package manifests for cache fingerprinting.
func recordLocalPackPackageInput(plan *authoringDependencyPlan, spec authoringPackageSpec) {
	// Skip remote PEP 508 specs because pack.yaml itself is already hashed.
	if spec.localPath == "" {
		return
	}
	// Hash pyproject.toml for editable local package directories and raw wheel bytes for .whl files.
	if spec.editable {
		localPyproject := filepath.Join(spec.localPath, "pyproject.toml")
		if regularFileExists(localPyproject) {
			plan.localProjFiles = append(plan.localProjFiles, localPyproject)
		}
		return
	}
	plan.wheelFiles = append(plan.wheelFiles, spec.localPath)
}

// parseAuthoringPackPackage validates one unified pack.yaml packages entry as a local path or PEP 508 requirement.
func parseAuthoringPackPackage(
	root, raw string, owned map[string]struct{},
) (authoringPackageSpec, error) {
	trimmed := strings.TrimSpace(raw)
	// Reject empty or multiline entries so requirements cannot inject extra CLI flags or lines.
	if trimmed == "" || strings.ContainsAny(trimmed, "\r\n") {
		return authoringPackageSpec{}, fmt.Errorf("invalid pack.yaml packages entry: %q", raw)
	}
	// Treat path-shaped entries as pack-relative local wheels or editable package directories.
	if looksLikePackPackagePath(trimmed) {
		return parseLocalAuthoringPackPackage(root, raw, trimmed)
	}
	// Block option-shaped requirement strings from being interpreted as uv flags.
	if strings.HasPrefix(trimmed, "-") {
		return authoringPackageSpec{}, fmt.Errorf("invalid pack.yaml packages entry: %q", raw)
	}
	// Enforce the same compiler-owned package boundary used during pack validation and agent sync.
	if err := rejectCompilerOwnedRequirement(trimmed, "pack.yaml", owned); err != nil {
		return authoringPackageSpec{}, err
	}
	return authoringPackageSpec{requirement: trimmed}, nil
}

// parseLocalAuthoringPackPackage validates one pack-relative .whl or editable package directory entry.
func parseLocalAuthoringPackPackage(root, raw, trimmed string) (authoringPackageSpec, error) {
	candidate := filepath.Join(root, filepath.FromSlash(trimmed))
	// Keep local package references strictly inside the pack directory.
	if !pathWithinDirectory(root, candidate) {
		return authoringPackageSpec{}, fmt.Errorf("pack.yaml packages entry must stay inside the pack directory: %q", raw)
	}
	info, err := os.Lstat(candidate)
	// Require declared local packages and wheels to exist on disk before invoking uv.
	if err != nil {
		return authoringPackageSpec{}, fmt.Errorf("inspect pack.yaml package %q: %w", raw, err)
	}
	// Reject symlinks so pack environments cannot pull in files outside the workspace.
	if info.Mode()&os.ModeSymlink != 0 {
		return authoringPackageSpec{}, fmt.Errorf("pack.yaml package cannot be a symlink: %q", raw)
	}
	// Install local Python package folders editably so editor changes take effect immediately.
	if info.IsDir() {
		return authoringPackageSpec{localPath: candidate, editable: true}, nil
	}
	// Install local wheel files directly; reject other arbitrary non-wheel files.
	if info.Mode().IsRegular() && strings.HasSuffix(candidate, ".whl") {
		return authoringPackageSpec{localPath: candidate}, nil
	}
	return authoringPackageSpec{}, fmt.Errorf("pack.yaml local package must be a directory or .whl file: %q", raw)
}

// looksLikePackPackagePath mirrors Python's _looks_like_pack_path heuristic for pack.yaml packages entries.
func looksLikePackPackagePath(value string) bool {
	return strings.ContainsAny(value, `/\`) || strings.HasSuffix(value, ".whl") || strings.HasPrefix(value, ".")
}

// discoverPackExtensionDirectories merges extensions listed in pack.yaml with canonical extensions/<name> folders.
func discoverPackExtensionDirectories(root string, declared []string) ([]string, error) {
	seen := make(map[string]struct{})
	var directories []string
	for _, relative := range declared {
		trimmed := strings.TrimSpace(relative)
		// Reject blank entries in pack.yaml extensions rather than resolving them to the pack root.
		if trimmed == "" {
			return nil, fmt.Errorf("pack.yaml extensions entry cannot be empty")
		}
		candidate := filepath.Join(root, filepath.FromSlash(trimmed))
		// Keep declared extension folders strictly within the pack root.
		if !pathWithinDirectory(root, candidate) {
			return nil, fmt.Errorf("pack.yaml extension path must stay inside the pack directory: %q", relative)
		}
		directories = appendUniquePath(directories, seen, candidate)
	}
	var err error
	directories, err = scanPackExtensionsSubdir(root, seen, directories)
	if err != nil {
		return nil, err
	}
	sort.Strings(directories)
	return directories, nil
}

// scanPackExtensionsSubdir discovers canonical extension.yaml folders under <pack>/extensions/.
func scanPackExtensionsSubdir(root string, seen map[string]struct{}, directories []string) ([]string, error) {
	extensionsRoot := filepath.Join(root, "extensions")
	entries, err := optionalRegularDirectoryEntries(extensionsRoot)
	if err != nil {
		return nil, err
	}
	for _, entry := range entries {
		// Skip hidden, private, and non-directory entries under extensions/.
		if strings.HasPrefix(entry.Name(), ".") || strings.HasPrefix(entry.Name(), "_") || !entry.IsDir() {
			continue
		}
		candidate := filepath.Join(extensionsRoot, entry.Name())
		// Only treat directories with extension.yaml as canonical Harnest Extensions.
		if regularFileExists(filepath.Join(candidate, "extension.yaml")) {
			directories = appendUniquePath(directories, seen, candidate)
		}
	}
	return directories, nil
}

// appendUniquePath adds candidate to paths once when not already recorded in seen.
func appendUniquePath(paths []string, seen map[string]struct{}, candidate string) []string {
	if _, exists := seen[candidate]; exists {
		return paths
	}
	seen[candidate] = struct{}{}
	return append(paths, candidate)
}

// collectExtensionAuthoringInputs validates one extension's manifest and optional pyproject.toml dependencies.
func collectExtensionAuthoringInputs(
	plan *authoringDependencyPlan, extensionRoot string, owned map[string]struct{},
) error {
	manifest, err := readLocalExtensionManifest(extensionRoot)
	// Require a valid extension.yaml and extension.py before installing its dependencies.
	if err != nil {
		return err
	}
	plan.manifestFiles = append(plan.manifestFiles, filepath.Join(extensionRoot, "extension.yaml"))
	// Enforce extension pyproject.toml name/version alignment with extension.yaml when present.
	if err := validateLocalExtensionProject(extensionRoot, manifest); err != nil {
		return err
	}
	pyprojectPath := filepath.Join(extensionRoot, "pyproject.toml")
	// Extensions without extra third-party dependencies may omit pyproject.toml completely.
	if !regularFileExists(pyprojectPath) {
		return nil
	}
	requirements, err := projectRuntimeRequirements(pyprojectPath, "Harnest Extension")
	if err != nil {
		return err
	}
	// Reject compiler-owned packages such as harnest or google-adk inside extension pyproject.toml.
	if err := rejectCompilerOwnedRequirements(requirements, pyprojectPath, owned); err != nil {
		return err
	}
	for _, requirement := range requirements {
		trimmed := strings.TrimSpace(requirement)
		// Block empty or flag-like dependency strings before passing them to uv pip install.
		if trimmed == "" || strings.HasPrefix(trimmed, "-") || strings.ContainsAny(trimmed, "\r\n") {
			return fmt.Errorf("invalid Harnest Extension dependency in %s: %q", pyprojectPath, requirement)
		}
		plan.requirements = append(plan.requirements, trimmed)
	}
	plan.manifestFiles = append(plan.manifestFiles, pyprojectPath)
	return nil
}

// authoringEnvironmentFingerprint hashes all authoring environment inputs so unchanged syncs hit cache.
func authoringEnvironmentFingerprint(
	plan authoringDependencyPlan, wheel runtimewheel.Artifact,
) (string, error) {
	digest := sha256.New()
	for _, value := range []string{
		"authoring-1",
		string(plan.kind),
		uvbootstrap.SupportedPythonSpec,
		strings.Join(plan.extras, ","),
		wheel.Name,
	} {
		digest.Write([]byte(value))
		digest.Write([]byte{0})
	}
	digest.Write(wheel.Contents)
	digest.Write([]byte{0})
	for _, path := range plan.manifestFiles {
		if err := hashEnvironmentDependencyInput(digest, plan.root, path, false); err != nil {
			return "", err
		}
	}
	for _, path := range plan.localProjFiles {
		if err := hashEnvironmentDependencyInput(digest, plan.root, path, false); err != nil {
			return "", err
		}
	}
	for _, wheelPath := range plan.wheelFiles {
		if err := hashAuthoringBinaryInput(digest, plan.root, wheelPath); err != nil {
			return "", err
		}
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

// hashAuthoringBinaryInput streams a local wheel into the fingerprint without a 1 MiB file-size cap.
func hashAuthoringBinaryInput(digest io.Writer, root, path string) error {
	relative, err := filepath.Rel(root, path)
	if err != nil {
		return fmt.Errorf("resolve local wheel path %s: %w", path, err)
	}
	file, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("open local wheel %s: %w", relative, err)
	}
	defer file.Close()
	if _, err := digest.Write([]byte(filepath.ToSlash(relative))); err != nil {
		return err
	}
	if _, err := digest.Write([]byte{0}); err != nil {
		return err
	}
	if _, err := io.Copy(digest, file); err != nil {
		return fmt.Errorf("hash local wheel %s: %w", relative, err)
	}
	_, err = digest.Write([]byte{0})
	return err
}

// installAuthoringEnvironment creates .harnest/environments/authoring and installs Harnest plus project deps.
func (a *application) installAuthoringEnvironment(
	command *cobra.Command,
	plan authoringDependencyPlan,
	wheel runtimewheel.Artifact,
	paths environmentPaths,
	fingerprint string,
) (pythonSelection, error) {
	relative, directory, err := prepareAuthoringSlot(paths)
	if err != nil {
		return pythonSelection{}, err
	}
	// Prefer a system-installed uv binary before falling back to the embedded uv artifact.
	uvPath, cleanupUV, err := a.resolveUV()
	if err != nil {
		return pythonSelection{}, fmt.Errorf("load uv: %w", err)
	}
	defer cleanupUV()
	wheelPath, cleanupWheel, err := stageRuntimeWheel(wheel)
	if err != nil {
		return pythonSelection{}, err
	}
	defer cleanupWheel()
	environment, err := a.authoringEnvironmentVariables(directory)
	if err != nil {
		return pythonSelection{}, err
	}
	python, err := a.runAuthoringUVInstall(command, uvPath, wheelPath, directory, environment, plan)
	if err != nil {
		return pythonSelection{}, err
	}
	if err := writeEnvironmentState(paths.state, environmentState{
		Fingerprint: fingerprint,
		Directory:   relative,
	}); err != nil {
		return pythonSelection{}, err
	}
	return pythonSelection{Executable: python, Source: string(plan.kind) + " environment"}, nil
}

// prepareAuthoringSlot validates the authoring venv path and clears any stale cache state before mutation.
func prepareAuthoringSlot(paths environmentPaths) (string, string, error) {
	relative := filepath.ToSlash(filepath.Join("environments", authoringEnvironmentSlot))
	directory := filepath.Join(paths.root, filepath.FromSlash(relative))
	// Reject a symlinked authoring slot so uv venv --clear never modifies a path outside .harnest/.
	if info, statErr := os.Lstat(directory); statErr == nil && info.Mode()&os.ModeSymlink != 0 {
		return "", "", fmt.Errorf("authoring environment cannot be a symlink: %s", directory)
	} else if statErr != nil && !os.IsNotExist(statErr) {
		return "", "", fmt.Errorf("inspect authoring environment: %w", statErr)
	}
	// Remove stale publication state before mutating the slot so a failed sync never looks cached.
	if err := os.Remove(paths.state); err != nil && !os.IsNotExist(err) {
		return "", "", fmt.Errorf("invalidate authoring environment: %w", err)
	}
	return relative, directory, nil
}

// runAuthoringUVInstall runs uv venv, pip install, and pip check pinned outside the cleared target directory.
func (a *application) runAuthoringUVInstall(
	command *cobra.Command,
	uvPath, wheelPath, directory string,
	environment []string,
	plan authoringDependencyPlan,
) (string, error) {
	workingDirectory := runtimeCommandWorkingDirectory(directory, uvPath)
	// Create a clean venv outside directory so --clear never unlinks uv's working directory.
	if err := a.runRuntimeCommandInDirectory(
		command,
		workingDirectory,
		environment,
		uvPath,
		"venv",
		"--python", uvbootstrap.SupportedPythonSpec,
		"--python-preference", "system",
		"--clear",
		directory,
	); err != nil {
		return "", fmt.Errorf("create %s virtual environment: %w", plan.kind, err)
	}
	python := runtimePythonPath(directory)
	// Install Harnest, framework extras, pack packages, and extension dependencies in one solve.
	if err := a.runRuntimeCommandInDirectory(
		command, workingDirectory, environment, uvPath, authoringInstallArguments(python, wheelPath, plan)...,
	); err != nil {
		return "", fmt.Errorf("install %s authoring dependencies: %w", plan.kind, err)
	}
	// Verify dependency compatibility so version conflicts surface immediately during env sync.
	if err := a.runRuntimeCommandInDirectory(
		command, workingDirectory, environment, uvPath, "pip", "check", "--python", python,
	); err != nil {
		return "", fmt.Errorf("verify %s authoring dependency compatibility: %w", plan.kind, err)
	}
	return python, nil
}

// authoringInstallArguments builds the single uv pip install invocation for a pack or extension workspace.
func authoringInstallArguments(python, wheelPath string, plan authoringDependencyPlan) []string {
	wheelSpec := fmt.Sprintf("harnest[%s] @ %s", strings.Join(plan.extras, ","), runtimeWheelURI(wheelPath))
	arguments := []string{"pip", "install", "--python", python, wheelSpec}
	for _, pkg := range plan.packages {
		// Install local package directories editably, local wheels directly, and PyPI specs verbatim.
		switch {
		case pkg.editable:
			arguments = append(arguments, "-e", pkg.localPath)
		case pkg.localPath != "":
			arguments = append(arguments, pkg.localPath)
		default:
			arguments = append(arguments, pkg.requirement)
		}
	}
	arguments = append(arguments, plan.requirements...)
	return arguments
}

// authoringEnvironmentVariables isolates uv settings while sharing Harnest's managed Python download cache.
func (a *application) authoringEnvironmentVariables(directory string) ([]string, error) {
	runtimeDirectory, err := a.runtimeDirectory("")
	if err != nil {
		return nil, err
	}
	return a.uvEnvironment(map[string]string{
		"UV_PROJECT_ENVIRONMENT": directory,
		"UV_PYTHON_INSTALL_DIR":  managedPythonDirectory(runtimeDirectory),
	}), nil
}

// publishIDEEnvironmentPointers creates/updates .venv and .vscode/settings.json for any managed project root.
func publishIDEEnvironmentPointers(command *cobra.Command, projectRoot, python string) {
	idePath, err := syncIDEEnvironmentLink(projectRoot, python)
	// Preserve usability of the managed runtime when the user owns .venv or links are unavailable.
	if err != nil {
		fmt.Fprintf(command.ErrOrStderr(), "IDE environment unchanged: %v\n", err)
	} else {
		fmt.Fprintf(command.OutOrStdout(), "IDE environment ready: %s\n", idePath)
	}
	settingsPath, err := syncVSCodeInterpreterSettings(projectRoot, python)
	// Avoid failing env sync if the user's .vscode/settings.json is malformed or read-only.
	if err != nil {
		fmt.Fprintf(command.ErrOrStderr(), "VS Code-compatible editor settings unchanged: %v\n", err)
		return
	}
	fmt.Fprintf(command.OutOrStdout(), "VS Code-compatible editor settings: %s\n", settingsPath)
}
