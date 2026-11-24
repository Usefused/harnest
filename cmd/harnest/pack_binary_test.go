// Tests standalone branded pack binaries: branding, commands, IDE skills, protected packages, and lock binding.

package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"harnest.dev/harnest/internal/packbinary"
	"harnest.dev/harnest/internal/runtimewheel"
)

// writePackFixtureFile creates parent directories and writes one regular test fixture file.
func writePackFixtureFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// makeLoadedPackFixture materializes a complete fake pack binary directory for CLI tests.
func makeLoadedPackFixture(t *testing.T, skills []packbinary.SkillEntry) *packbinary.LoadedPack {
	t.Helper()
	root := t.TempDir()
	packDir := filepath.Join(root, "pack")
	if err := createProjectPackScaffold(packDir, "acme"); err != nil {
		t.Fatal(err)
	}
	writePackFixtureFile(t, filepath.Join(root, "wheels", "acme_internal-1.0.0-py3-none-any.whl"), "wheel")
	writePackFixtureFile(t, filepath.Join(root, "extensions", "acme-guard", "extension.yaml"), "apiVersion: harnest.dev/v1alpha1\nkind: Extension\n")
	writePackFixtureFile(t, filepath.Join(root, "extensions", "acme-guard", "pyproject.toml"), "[project]\nname = 'harnest-extension-acme-guard'\nversion = '1.0.0'\ndependencies = ['httpx>=0.28']\n")
	for _, skill := range skills {
		body := "---\nname: " + skill.Name + "\ndescription: Company skill " + skill.Name + ".\n---\n# Custom " + skill.Name + "\n"
		writePackFixtureFile(t, filepath.Join(root, filepath.FromSlash(skill.Root), "SKILL.md"), body)
	}
	return &packbinary.LoadedPack{
		Root: root,
		Manifest: packbinary.Manifest{
			Format:            packbinary.Format,
			Name:              "acme",
			Version:           "2.0.0",
			Title:             "Acme agents",
			Command:           "acme",
			HasProjectHooks:   true,
			Commands:          []packbinary.CommandEntry{{Name: "whoami", Help: "Print owning team"}},
			Skills:            skills,
			Wheels:            []packbinary.WheelEntry{{Name: "acme-internal", Filename: "wheels/acme_internal-1.0.0-py3-none-any.whl", SHA256: strings.Repeat("a", 64)}},
			Requirements:      []string{"acme-sdk==2.1.0"},
			ProtectedPackages: []string{"acme-internal", "acme-sdk"},
			Extensions:        []packbinary.ExtensionEntry{{Name: "acme-guard", Path: "extensions/acme-guard", HasPyproject: true}},
			Digest:            strings.Repeat("f", 64),
		},
	}
}

// TestBrandedPackBinaryRoutesCommandsAndSkipsCoreInitRecursion verifies CLI branding, custom commands, and init/upgrade routing.
func TestBrandedPackBinaryRoutesCommandsAndSkipsCoreInitRecursion(t *testing.T) {
	loaded := makeLoadedPackFixture(t, nil)
	sys := defaultSystem()
	sys.embeddedPack = loaded
	root := newRootCommand(sys, "2.2.0")
	if root.Use != "acme" || !strings.Contains(root.Version, "2.0.0") {
		t.Fatalf("unexpected branded root: use=%q version=%q", root.Use, root.Version)
	}
	python, record := recordingPython(t)
	t.Setenv("HARNEST_PYTHON", python)
	if _, _, err := executeForTest(t, sys, "whoami", "--strict", "./agent"); err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "branded custom command", string(mustReadTestFile(t, record)), []string{
		"pack\nrun\n" + loaded.PackDir() + "\nwhoami\n--strict\n./agent",
	})
	agent := filepath.Join(t.TempDir(), "my-agent")
	if _, _, err := executeForTest(t, sys, "--python", python, "init", agent, "--dry-run"); err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "branded init", string(mustReadTestFile(t, record)), []string{
		"pack\ninit\n" + agent, "--pack\n" + loaded.PackDir(), "--dry-run",
	})
	// When planner.py invokes core init with HARNEST_PACK_CORE_INIT=1, branded init must not recurse.
	t.Setenv(packCoreInitEnv, "1")
	coreAgent := filepath.Join(t.TempDir(), "core-agent")
	if _, _, err := executeForTest(t, sys, "init", coreAgent, "--minimal"); err != nil {
		t.Fatalf("core init recursion guard failed: %v", err)
	}
	assertFilesExist(t, coreAgent, []string{"config.yaml", "agent.py"})
}

// TestBrandedPackBinarySkillsAdditiveAndOverride verifies pack IDE skills merge with and override Harnest built-ins.
func TestBrandedPackBinarySkillsAdditiveAndOverride(t *testing.T) {
	loaded := makeLoadedPackFixture(t, []packbinary.SkillEntry{
		{Name: "acme-standards", Root: "skills/acme-standards"},
		{Name: authoringSkillName, Root: "skills/" + authoringSkillName},
	})
	sys := defaultSystem()
	sys.embeddedPack = loaded

	stdout, _, err := executeForTest(t, sys, "skills", "show", "acme-standards")
	if err != nil || !strings.Contains(stdout, "# Custom acme-standards") {
		t.Fatalf("expected custom additive skill output, got %q err=%v", stdout, err)
	}
	overrideOut, _, err := executeForTest(t, sys, "skills", "show", authoringSkillName)
	if err != nil || !strings.Contains(overrideOut, "# Custom "+authoringSkillName) {
		t.Fatalf("expected overridden built-in skill, got %q err=%v", overrideOut, err)
	}
	project := t.TempDir()
	if _, _, err := executeForTest(t, sys, "skills", "install", "--target", "claude", "--project", project); err != nil {
		t.Fatalf("skills install failed: %v", err)
	}
	assertFilesExist(t, project, []string{
		".claude/skills/acme-standards/SKILL.md",
		".claude/skills/" + authoringSkillName + "/SKILL.md",
		".claude/skills/" + authenticationSkillName + "/SKILL.md",
	})
	installedAuthoring := string(mustReadTestFile(t, filepath.Join(project, ".claude", "skills", authoringSkillName, "SKILL.md")))
	if !strings.Contains(installedAuthoring, "# Custom "+authoringSkillName) {
		t.Fatalf("installed authoring skill did not use pack override: %s", installedAuthoring)
	}
}

// TestPackProtectedPackagesRejectsOverrides verifies agent pyproject cannot override pack wheels or requirements.
func TestPackProtectedPackagesRejectsOverrides(t *testing.T) {
	loaded := makeLoadedPackFixture(t, nil)
	agentDir := filepath.Join(t.TempDir(), "protected-agent")
	if _, _, err := executeForTest(t, defaultSystem(), "init", agentDir, "--minimal"); err != nil {
		t.Fatal(err)
	}
	bundle, err := loadAgentBundle(agentDir)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateAgentDependencyPolicyWithPack(bundle, loaded); err != nil {
		t.Fatalf("clean agent rejected by pack policy: %v", err)
	}
	pyproject := filepath.Join(agentDir, "pyproject.toml")
	mustWriteEnvironmentFixture(t, pyproject, "[project]\nname = 'protected-agent'\nversion = '0.1.0'\ndependencies = ['acme_internal==0.1.0']\n")
	if err := validateAgentDependencyPolicyWithPack(bundle, loaded); err == nil || !strings.Contains(err.Error(), "pack-owned package") {
		t.Fatalf("expected pack-owned wheel override rejection, got %v", err)
	}
	mustWriteEnvironmentFixture(t, pyproject, "[project]\nname = 'protected-agent'\nversion = '0.1.0'\ndependencies = ['acme-sdk>=1.0']\n")
	if err := validateAgentDependencyPolicyWithPack(bundle, loaded); err == nil || !strings.Contains(err.Error(), "upgrade acme") {
		t.Fatalf("expected pack-owned requirement override rejection, got %v", err)
	}
}

// TestPackLockBindingAndWheelMarker verifies lock fingerprint invalidation and portable pack wheel markers.
func TestPackLockBindingAndWheelMarker(t *testing.T) {
	loaded := makeLoadedPackFixture(t, nil)
	agentDir := filepath.Join(t.TempDir(), "lock-agent")
	if _, _, err := executeForTest(t, defaultSystem(), "init", agentDir, "--minimal"); err != nil {
		t.Fatal(err)
	}
	bundle, err := loadAgentBundle(agentDir)
	if err != nil {
		t.Fatal(err)
	}
	plan, err := inspectRuntimeDependencyPlan(bundle)
	if err != nil {
		t.Fatal(err)
	}
	wheel := runtimewheel.Artifact{Name: "harnest-2.2.0-py3-none-any.whl", Contents: []byte("wheel")}
	plainFP, err := runtimeLockInputFingerprint(bundle, wheel, plan, runtimeEnvironmentProfile)
	if err != nil {
		t.Fatal(err)
	}
	attachEmbeddedPackPlan(&plan, loaded)
	packFP, err := runtimeLockInputFingerprint(bundle, wheel, plan, runtimeEnvironmentProfile)
	if err != nil || packFP == plainFP {
		t.Fatalf("pack digest did not invalidate lock fingerprint: plain=%s pack=%s err=%v", plainFP, packFP, err)
	}
	lines := strings.Join(packLockInputLines(plan), "\n")
	assertContainsAll(t, "pack lock inputs", lines, []string{"acme-internal @ file://", "acme-sdk==2.1.0"})
	lockBody := "acme-internal @ " + packWheelsLockMarker + "/acme_internal-1.0.0-py3-none-any.whl\n"
	if _, err := substitutePackWheelMarker(lockBody, ""); err == nil {
		t.Fatal("accepted pack wheel marker without branded pack binary")
	}
	rendered, err := substitutePackWheelMarker(lockBody, loaded.WheelsDir())
	if err != nil || !strings.Contains(rendered, "file://") {
		t.Fatalf("failed to restore pack wheel URI: %q err=%v", rendered, err)
	}
}

// packBinaryTestPython resolves the test Python interpreter or skips when unavailable.
func packBinaryTestPython(t *testing.T) string {
	t.Helper()
	pythonPath := strings.TrimSpace(os.Getenv("HARNEST_PYTHON"))
	// Fall back to the repository's local virtual environment when HARNEST_PYTHON is unset.
	if pythonPath == "" {
		pythonPath = filepath.Join("..", "..", ".venv", "bin", "python")
	}
	if _, err := os.Stat(pythonPath); err != nil {
		t.Skip("local Python environment unavailable for end-to-end pack binary build")
	}
	absPython, err := filepath.Abs(pythonPath)
	if err != nil {
		t.Fatal(err)
	}
	return absPython
}

// TestPackBuildDefaultBinaryProducesExecutableBrandedCLI exercises real binary stamping and execution end to end.
func TestPackBuildDefaultBinaryProducesExecutableBrandedCLI(t *testing.T) {
	absPython := packBinaryTestPython(t)
	packDir := filepath.Join(t.TempDir(), "acme-pack")
	if err := createProjectPackScaffold(packDir, "acme"); err != nil {
		t.Fatal(err)
	}
	writePackFixtureFile(
		t,
		filepath.Join(packDir, "coding-skills", "acme-standards", "SKILL.md"),
		"---\nname: acme-standards\ndescription: Acme coding skill.\n---\n# Acme Standards\n",
	)
	writePackFixtureFile(
		t,
		filepath.Join(packDir, "pack.yaml"),
		"apiVersion: harnest.dev/pack/v1\nname: acme\nversion: 1.4.0\ntitle: Acme agents\ncommand: acme\nskills:\n  - coding-skills/acme-standards\n",
	)
	baseBin := filepath.Join(t.TempDir(), "harnest-base")
	buildBase := defaultSystem().commandContext(t.Context(), "go", "build", "-o", baseBin, ".")
	if output, err := buildBase.CombinedOutput(); err != nil {
		t.Fatalf("go build base harnest failed: %v\n%s", err, output)
	}
	distDir := filepath.Join(t.TempDir(), "dist")
	stdout, stderr, err := executeForTest(
		t, defaultSystem(), "--python", absPython, "pack", "build", packDir, "--base-binary", baseBin, "--output", distDir,
	)
	if err != nil {
		t.Fatalf("harnest pack build failed: %v\nstdout=%s\nstderr=%s", err, stdout, stderr)
	}
	stamped := filepath.Join(distDir, "acme")
	assertFilesExist(t, distDir, []string{"acme"})

	// Execute the stamped standalone binary directly with an isolated pack cache.
	cacheDir := t.TempDir()
	versionCmd := defaultSystem().commandContext(t.Context(), stamped, "--version")
	versionCmd.Env = append(os.Environ(), packCacheOverrideEnv+"="+cacheDir)
	versionOut, err := versionCmd.CombinedOutput()
	if err != nil || !strings.Contains(string(versionOut), "acme version 1.4.0") {
		t.Fatalf("stamped acme --version failed: err=%v out=%q", err, versionOut)
	}
	skillCmd := defaultSystem().commandContext(t.Context(), stamped, "skills", "show", "acme-standards")
	skillCmd.Env = append(os.Environ(), packCacheOverrideEnv+"="+cacheDir)
	skillOut, err := skillCmd.CombinedOutput()
	if err != nil || !strings.Contains(string(skillOut), "# Acme Standards") {
		t.Fatalf("stamped acme skills show failed: err=%v out=%q", err, skillOut)
	}
}

// TestPackEnvSyncResolvesBundledWheelsRequirementsAndExtensions verifies env sync passes pack inputs to uv and normalizes lock markers.
func TestPackEnvSyncResolvesBundledWheelsRequirementsAndExtensions(t *testing.T) {
	loaded := makeLoadedPackFixture(t, nil)
	root, agentDir := scaffoldIDEEnvironmentTestAgent(t)
	callsPath := filepath.Join(root, "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", callsPath)
	sys := environmentTestSystem(t, root)
	sys.embeddedPack = loaded
	if _, _, err := executeForTest(t, sys, "env", "sync", agentDir); err != nil {
		t.Fatalf("env sync with embedded pack failed: %v", err)
	}
	calls := string(mustReadTestFile(t, callsPath))
	assertContainsAll(t, "uv compile pack inputs", calls, []string{
		"acme-internal @ file://",
		"acme-sdk==2.1.0",
		loaded.ExtensionProjectFiles()[0],
	})
	if _, _, err := executeForTest(t, sys, "env", "sync", agentDir, "--frozen"); err != nil {
		t.Fatalf("frozen env sync with embedded pack failed: %v", err)
	}
	// Plain Harnest without the embedded pack must reject the pack-bound lock as stale.
	plainSys := environmentTestSystem(t, root)
	if _, _, err := executeForTest(t, plainSys, "env", "sync", agentDir, "--frozen"); err == nil {
		t.Fatal("plain Harnest accepted a frozen lock bound to an embedded pack")
	}
}
