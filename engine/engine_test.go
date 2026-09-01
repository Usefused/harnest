package engine

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
)

func TestDiscoverLoadsEnabledBundlesInStableOrder(t *testing.T) {
	project := t.TempDir()
	writeAgent(t, project, "zeta", true)
	writeAgent(t, project, "alpha", true)
	writeAgent(t, project, "disabled", false)
	plan := testPlan(project)
	plan.Labels = map[string]string{"environment": "test"}

	bundles, err := Discover(plan)
	if err != nil {
		t.Fatal(err)
	}
	if len(bundles) != 2 {
		t.Fatalf("got %d bundles, want 2", len(bundles))
	}
	if bundles[0].Config.Metadata.Name != "alpha" || bundles[1].Config.Metadata.Name != "zeta" {
		t.Fatalf("bundles not sorted: %v, %v", bundles[0].Config.Metadata.Name, bundles[1].Config.Metadata.Name)
	}
	if len(bundles[0].Digest) != len("sha256:")+64 {
		t.Fatalf("unexpected digest %q", bundles[0].Digest)
	}
	if bundles[0].Labels["environment"] != "test" {
		t.Fatalf("deployment labels were not propagated: %v", bundles[0].Labels)
	}
}

func TestDiscoverRejectsUnknownConfigFields(t *testing.T) {
	project := t.TempDir()
	directory := writeAgent(t, project, "broken", true)
	file, err := os.OpenFile(filepath.Join(directory, "config.yaml"), os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = file.WriteString("unknownField: true\n")
	_ = file.Close()

	if _, err := Discover(testPlan(project)); err == nil {
		t.Fatal("expected strict YAML error")
	}
}

func TestLoadBundleReadsExplicitCLIInterfaceOptIn(t *testing.T) {
	project := t.TempDir()
	directory := writeAgent(t, project, "cli-agent", true)
	file, err := os.OpenFile(filepath.Join(directory, "config.yaml"), os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.WriteString("  interfaces:\n    cli: true\n"); err != nil {
		_ = file.Close()
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	bundle, err := LoadBundle(directory)
	if err != nil {
		t.Fatal(err)
	}
	if !bundle.Config.Spec.Interfaces.CLI {
		t.Fatal("CLI interface opt-in was not loaded")
	}
}

func TestDeployAllCallsDeployerForEachAgent(t *testing.T) {
	project := t.TempDir()
	writeAgent(t, project, "alpha", true)
	writeAgent(t, project, "beta", true)
	deployer := &recordingDeployer{}

	if err := DeployAll(context.Background(), testPlan(project), deployer); err != nil {
		t.Fatal(err)
	}
	deployer.mu.Lock()
	defer deployer.mu.Unlock()
	if len(deployer.names) != 2 {
		t.Fatalf("deployed %v, want two agents", deployer.names)
	}
}

func TestDecodePlanRejectsUnknownFields(t *testing.T) {
	input := bytes.NewBufferString(`{"apiVersion":"harnest.dev/v1alpha1","kind":"DeploymentPlan","projectRoot":"/tmp","parallelism":1,"sources":[{"root":"agents"}],"surprise":true}`)
	if _, err := DecodePlan(input); err == nil {
		t.Fatal("expected unknown field error")
	}
}

func TestDecodePlanRejectsTrailingJSONValue(t *testing.T) {
	input := bytes.NewBufferString(`{"apiVersion":"harnest.dev/v1alpha1","kind":"DeploymentPlan","projectRoot":"/tmp","parallelism":1,"sources":[{"root":"agents"}]} {}`)
	if _, err := DecodePlan(input); err == nil {
		t.Fatal("expected trailing JSON value error")
	}
}

func TestDiscoverRejectsSourceSymlinkOutsideProject(t *testing.T) {
	project := t.TempDir()
	external := t.TempDir()
	writeAgent(t, external, "outside", true)
	if err := os.Symlink(filepath.Join(external, "agents"), filepath.Join(project, "agents")); err != nil {
		t.Fatal(err)
	}

	if _, err := Discover(testPlan(project)); err == nil {
		t.Fatal("expected source symlink escape error")
	}
}

func TestLoadBundleRejectsDependencyProjectSymlink(t *testing.T) {
	project := t.TempDir()
	directory := writeAgent(t, project, "unsafe", true)
	dependencyProject := filepath.Join(directory, "pyproject.toml")
	if err := os.Remove(dependencyProject); err != nil {
		t.Fatal(err)
	}
	external := filepath.Join(t.TempDir(), "pyproject.toml")
	mustWrite(t, external, "external-package\n")
	if err := os.Symlink(external, dependencyProject); err != nil {
		t.Fatal(err)
	}

	if _, err := LoadBundle(directory); err == nil {
		t.Fatal("expected dependency project symlink error")
	}
}

func TestLoadBundleRequiresNonEmptyInstructions(t *testing.T) {
	for _, testCase := range []struct {
		name         string
		instructions *string
	}{
		{name: "missing", instructions: nil},
		{name: "empty", instructions: stringPointer(" \n\t")},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			project := t.TempDir()
			directory := writeAgent(t, project, "instructions", true)
			path := filepath.Join(directory, "instructions.md")
			if testCase.instructions == nil {
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
			} else {
				mustWrite(t, path, *testCase.instructions)
			}
			if _, err := LoadBundle(directory); err == nil || !strings.Contains(err.Error(), "instructions.md") {
				t.Fatalf("got error %v, want instructions.md validation error", err)
			}
		})
	}
}

func TestLoadBundleRejectsSymlinksInConventionDirectories(t *testing.T) {
	project := t.TempDir()
	directory := writeAgent(t, project, "unsafe-resources", true)
	skills := filepath.Join(directory, "skills")
	if err := os.MkdirAll(skills, 0o755); err != nil {
		t.Fatal(err)
	}
	external := filepath.Join(t.TempDir(), "SKILL.md")
	mustWrite(t, external, "External instructions.\n")
	if err := os.Symlink(external, filepath.Join(skills, "SKILL.md")); err != nil {
		t.Fatal(err)
	}

	if _, err := LoadBundle(directory); err == nil || !strings.Contains(err.Error(), "must not be a symlink") {
		t.Fatalf("got error %v, want resource symlink validation error", err)
	}
}

func TestLoadBundleRejectsSymlinksInLibraryDirectory(t *testing.T) {
	project := t.TempDir()
	directory := writeAgent(t, project, "unsafe-library", true)
	library := filepath.Join(directory, "lib")
	if err := os.MkdirAll(library, 0o755); err != nil {
		t.Fatal(err)
	}
	external := filepath.Join(t.TempDir(), "shared.py")
	mustWrite(t, external, "def shared():\n    return 'external'\n")
	if err := os.Symlink(external, filepath.Join(library, "shared.py")); err != nil {
		t.Fatal(err)
	}

	if _, err := LoadBundle(directory); err == nil || !strings.Contains(err.Error(), "must not be a symlink") {
		t.Fatalf("got error %v, want library symlink validation error", err)
	}
}

func TestLoadBundleRejectsLegacyMCPServersDirectory(t *testing.T) {
	project := t.TempDir()
	directory := writeAgent(t, project, "legacy-mcp", true)
	if err := os.Mkdir(filepath.Join(directory, "mcp_servers"), 0o755); err != nil {
		t.Fatal(err)
	}

	if _, err := LoadBundle(directory); err == nil || !strings.Contains(err.Error(), "use mcp") {
		t.Fatalf("got error %v, want mcp directory migration error", err)
	}
}

func TestLoadBundleUsesAdvancedModeAndRejectsRemovedNativeMode(t *testing.T) {
	project := t.TempDir()
	directory := writeAgent(t, project, "advanced-mode", true)
	configPath := filepath.Join(directory, "config.yaml")
	contents, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	advanced := strings.Replace(string(contents), "mode: managed", "mode: advanced", 1)
	mustWrite(t, configPath, advanced)
	bundle, err := LoadBundle(directory)
	if err != nil {
		t.Fatalf("advanced mode should be valid: %v", err)
	}
	if bundle.Config.Spec.Framework.EffectiveMode() != "advanced" {
		t.Fatalf("unexpected framework mode %#v", bundle.Config.Spec.Framework)
	}

	removed := strings.Replace(advanced, "mode: advanced", "mode: native", 1)
	mustWrite(t, configPath, removed)
	if _, err := LoadBundle(directory); err == nil || !strings.Contains(err.Error(), "managed or advanced") {
		t.Fatalf("got error %v, want removed native-mode validation error", err)
	}
}

func TestBundleDigestIncludesSkillsAndEvals(t *testing.T) {
	project := t.TempDir()
	directory := writeAgent(t, project, "resources", true)
	mustWrite(t, filepath.Join(directory, "skills", "support", "SKILL.md"), "First skill version.\n")
	mustWrite(t, filepath.Join(directory, "evals", "cases.jsonl"), "{\"input\":\"hello\"}\n")
	first, err := LoadBundle(directory)
	if err != nil {
		t.Fatal(err)
	}
	if first.InstructionsPath != filepath.Join(first.Directory, "instructions.md") {
		t.Fatalf("unexpected instructions path %q", first.InstructionsPath)
	}
	mustWrite(t, filepath.Join(directory, "skills", "support", "SKILL.md"), "Second skill version.\n")
	second, err := LoadBundle(directory)
	if err != nil {
		t.Fatal(err)
	}
	if first.Digest == second.Digest {
		t.Fatal("skill content change did not alter bundle digest")
	}
}

func TestDeployAllValidatesPlanBeforeStartingWorkers(t *testing.T) {
	plan := testPlan(t.TempDir())
	plan.Parallelism = 0
	err := DeployAll(context.Background(), plan, &recordingDeployer{})
	if err == nil || !strings.Contains(err.Error(), "parallelism") {
		t.Fatalf("got error %v, want parallelism validation error", err)
	}
}

func TestDeployAllHonorsCanceledContext(t *testing.T) {
	project := t.TempDir()
	writeAgent(t, project, "alpha", true)
	deployer := &recordingDeployer{}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	err := DeployAll(ctx, testPlan(project), deployer)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("got error %v, want context cancellation", err)
	}
	deployer.mu.Lock()
	defer deployer.mu.Unlock()
	if len(deployer.names) != 0 {
		t.Fatalf("deployed %v after context cancellation", deployer.names)
	}
}

func TestDeployAllFailFastStopsPendingAgents(t *testing.T) {
	project := t.TempDir()
	writeAgent(t, project, "alpha", true)
	writeAgent(t, project, "beta", true)
	writeAgent(t, project, "gamma", true)
	plan := testPlan(project)
	plan.Parallelism = 1
	plan.FailFast = true
	deployer := &failingDeployer{}

	err := DeployAll(context.Background(), plan, deployer)
	if err == nil || !strings.Contains(err.Error(), "deploy alpha") {
		t.Fatalf("got error %v, want alpha deployment error", err)
	}
	deployer.mu.Lock()
	defer deployer.mu.Unlock()
	if fmt.Sprint(deployer.names) != "[alpha]" {
		t.Fatalf("deployed %v, want only first agent", deployer.names)
	}
}

func TestCompileAndDeployAllAttachesCompiledArtifact(t *testing.T) {
	project := t.TempDir()
	writeAgent(t, project, "alpha", true)
	compiler := &recordingCompiler{}
	deployer := &compiledRecordingDeployer{}

	if err := CompileAndDeployAll(context.Background(), testPlan(project), compiler, deployer); err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(compiler.names) != "[alpha]" {
		t.Fatalf("compiled %v, want alpha", compiler.names)
	}
	if fmt.Sprint(deployer.entrypoints) != "[agent:root_agent]" {
		t.Fatalf("deployed entrypoints %v", deployer.entrypoints)
	}
}

func TestLoadCompiledArtifactVerifiesManifestAndSource(t *testing.T) {
	project := t.TempDir()
	source, err := LoadBundle(writeAgent(t, project, "compiled", true))
	if err != nil {
		t.Fatal(err)
	}
	artifactDirectory := filepath.Join(t.TempDir(), "artifact")
	copyTree(t, source.Directory, filepath.Join(artifactDirectory, "source"))
	mustWrite(t, filepath.Join(artifactDirectory, "__init__.py"), "from .agent import root_agent\n")
	mustWrite(t, filepath.Join(artifactDirectory, "agent.py"), "root_agent = object()\n")
	mustWrite(t, filepath.Join(artifactDirectory, compiledServerConfigFilename), "kind: Server\n")
	manifest := compiledManifestForDirectory(t, artifactDirectory, source)
	manifestJSON, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	mustWrite(t, filepath.Join(artifactDirectory, compiledManifestFilename), string(manifestJSON))

	artifact, err := loadCompiledArtifact(artifactDirectory, source)
	if err != nil {
		t.Fatal(err)
	}
	if artifact.Manifest.Entrypoint != "agent:root_agent" {
		t.Fatalf("unexpected compiled entrypoint %q", artifact.Manifest.Entrypoint)
	}

	mismatch := manifest
	mismatch.Framework = CompiledFramework{
		Name: "langgraph", Mode: "managed", Distribution: "langgraph", Version: "1.2.0",
	}
	mismatchJSON, err := json.Marshal(mismatch)
	if err != nil {
		t.Fatal(err)
	}
	mustWrite(t, filepath.Join(artifactDirectory, compiledManifestFilename), string(mismatchJSON))
	if _, err := loadCompiledArtifact(artifactDirectory, source); err == nil || !strings.Contains(err.Error(), "does not match config") {
		t.Fatalf("got error %v, want framework mismatch", err)
	}
	mustWrite(t, filepath.Join(artifactDirectory, compiledManifestFilename), string(manifestJSON))

	mustWrite(t, filepath.Join(artifactDirectory, "source", "instructions.md"), "Tampered.\n")
	if _, err := loadCompiledArtifact(artifactDirectory, source); err == nil {
		t.Fatal("expected tampered compiled source to fail validation")
	}
}

func TestCompiledArtifactRejectsCLIInterfaceMismatch(t *testing.T) {
	source, directory := compiledServerArtifactFixture(t)
	path := filepath.Join(directory, compiledManifestFilename)
	manifest := readCompiledManifest(t, path)
	manifest.Interfaces.CLI = !source.Config.Spec.Interfaces.CLI
	manifest.Digest = compiledManifestDigest(
		manifest.Files,
		manifest.Interfaces,
		manifest.Plugins,
		manifest.Tasks,
		manifest.Crons,
		manifest.RuntimeDependencies,
	)
	writeCompiledManifest(t, path, manifest)
	if _, err := loadCompiledArtifact(directory, source); err == nil || !strings.Contains(err.Error(), "CLI interface") {
		t.Fatalf("got error %v, want CLI interface mismatch", err)
	}
}

func TestCompiledArtifactAllowsDistinctDNSDeploymentAndADKNames(t *testing.T) {
	project := t.TempDir()
	source, err := LoadBundle(writeAgent(t, project, "support-agent", true))
	if err != nil {
		t.Fatal(err)
	}
	artifactDirectory := filepath.Join(t.TempDir(), "artifact")
	copyTree(t, source.Directory, filepath.Join(artifactDirectory, "source"))
	mustWrite(t, filepath.Join(artifactDirectory, "__init__.py"), "from .agent import root_agent\n")
	mustWrite(t, filepath.Join(artifactDirectory, "agent.py"), "root_agent = object()\n")
	mustWrite(t, filepath.Join(artifactDirectory, compiledServerConfigFilename), "kind: Server\n")
	manifest := compiledManifestForDirectory(t, artifactDirectory, source)
	manifest.Name = "support_agent"
	manifestJSON, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	mustWrite(t, filepath.Join(artifactDirectory, compiledManifestFilename), string(manifestJSON))
	if _, err := loadCompiledArtifact(artifactDirectory, source); err != nil {
		t.Fatalf("valid distinct ADK name was rejected: %v", err)
	}

	manifest.Name = "support-agent"
	manifestJSON, err = json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	mustWrite(t, filepath.Join(artifactDirectory, compiledManifestFilename), string(manifestJSON))
	if _, err := loadCompiledArtifact(artifactDirectory, source); err == nil || !strings.Contains(err.Error(), "valid agent runtime name") {
		t.Fatalf("got error %v, want invalid runtime name rejection", err)
	}
}

func TestCompiledArtifactAllowsReplacedServerConfig(t *testing.T) {
	source, directory := compiledServerArtifactFixture(t)
	mustWrite(t, filepath.Join(directory, compiledServerConfigFilename), "kind: Server\nhttp:\n  port: 9090\n")
	if _, err := loadCompiledArtifact(directory, source); err != nil {
		t.Fatalf("mutable server config invalidated artifact: %v", err)
	}
}

func TestCompiledArtifactRequiresServerConfig(t *testing.T) {
	source, directory := compiledServerArtifactFixture(t)
	if err := os.Remove(filepath.Join(directory, compiledServerConfigFilename)); err != nil {
		t.Fatal(err)
	}
	if _, err := loadCompiledArtifact(directory, source); err == nil || !strings.Contains(err.Error(), "server config") {
		t.Fatalf("got error %v, want missing server config rejection", err)
	}
}

func TestCompiledArtifactRejectsServerConfigSymlink(t *testing.T) {
	source, directory := compiledServerArtifactFixture(t)
	serverConfig := filepath.Join(directory, compiledServerConfigFilename)
	if err := os.Remove(serverConfig); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(directory, "source", "config.yaml")
	if err := os.Symlink(target, serverConfig); err != nil {
		t.Fatal(err)
	}
	if _, err := loadCompiledArtifact(directory, source); err == nil || !strings.Contains(err.Error(), "not a regular file") {
		t.Fatalf("got error %v, want server config symlink rejection", err)
	}
}

func TestCompiledArtifactRejectsNonRegularServerConfig(t *testing.T) {
	source, directory := compiledServerArtifactFixture(t)
	serverConfig := filepath.Join(directory, compiledServerConfigFilename)
	if err := os.Remove(serverConfig); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(serverConfig, 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := loadCompiledArtifact(directory, source); err == nil || !strings.Contains(err.Error(), "not a regular file") {
		t.Fatalf("got error %v, want non-regular server config rejection", err)
	}
}

func TestCompiledArtifactRejectsOtherUnmanifestedFiles(t *testing.T) {
	source, directory := compiledServerArtifactFixture(t)
	mustWrite(t, filepath.Join(directory, "unexpected.txt"), "unexpected\n")
	if _, err := loadCompiledArtifact(directory, source); err == nil || !strings.Contains(err.Error(), "unmanifested") {
		t.Fatalf("got error %v, want unmanifested file rejection", err)
	}
}

func TestCompiledArtifactRejectsMismatchedCheckpointOwnership(t *testing.T) {
	source, directory := compiledServerArtifactFixture(t)
	path := filepath.Join(directory, compiledManifestFilename)
	contents, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var manifest CompiledManifest
	if err := json.Unmarshal(contents, &manifest); err != nil {
		t.Fatal(err)
	}
	manifest.Checkpoint = CompiledCheckpoint{
		Owner: "langgraph", Framework: "langgraph", Schema: "langgraph-native",
	}
	contents, err = json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	mustWrite(t, path, string(contents))
	if _, err := loadCompiledArtifact(directory, source); err == nil || !strings.Contains(err.Error(), "checkpoint ownership") {
		t.Fatalf("got error %v, want checkpoint ownership mismatch", err)
	}
}

func TestCompiledArtifactValidatesResolvedPluginGraph(t *testing.T) {
	source, directory := compiledServerArtifactFixture(t)
	path := filepath.Join(directory, compiledManifestFilename)
	contents, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var manifest CompiledManifest
	if err := json.Unmarshal(contents, &manifest); err != nil {
		t.Fatal(err)
	}
	manifest.Plugins = []CompiledPlugin{
		{Name: "clock", Version: "1.0.0", Digest: "sha256:" + strings.Repeat("a", 64), Capabilities: []string{"context.resources"}},
		{Name: "workflow", Version: "1.0.0", Digest: "sha256:" + strings.Repeat("b", 64), Requires: []string{"clock"}, Capabilities: []string{"lifecycle.tool"}},
	}
	manifest.Digest = compiledManifestDigest(manifest.Files, manifest.Interfaces, manifest.Plugins, nil, nil, nil)
	writeCompiledManifest(t, path, manifest)
	if _, err := loadCompiledArtifact(directory, source); err != nil {
		t.Fatalf("valid plugin graph was rejected: %v", err)
	}

	manifest.Plugins[0].Requires = []string{"workflow"}
	manifest.Digest = compiledManifestDigest(manifest.Files, manifest.Interfaces, manifest.Plugins, nil, nil, nil)
	writeCompiledManifest(t, path, manifest)
	if _, err := loadCompiledArtifact(directory, source); err == nil || !strings.Contains(err.Error(), "unresolved plugin") {
		t.Fatalf("got error %v, want unresolved plugin rejection", err)
	}

	manifest.Plugins[0].Requires = nil
	manifest.Plugins[0].Version = "01.0.0"
	manifest.Digest = compiledManifestDigest(manifest.Files, manifest.Interfaces, manifest.Plugins, nil, nil, nil)
	writeCompiledManifest(t, path, manifest)
	if _, err := loadCompiledArtifact(directory, source); err == nil || !strings.Contains(err.Error(), "semantic version") {
		t.Fatalf("got error %v, want semantic version rejection", err)
	}
}

func TestCompiledArtifactRejectsMutatedPluginProvenance(t *testing.T) {
	source, directory := compiledServerArtifactFixture(t)
	path := filepath.Join(directory, compiledManifestFilename)
	manifest := readCompiledManifest(t, path)
	manifest.Plugins = []CompiledPlugin{{
		Name: "clock", Version: "1.0.0", Digest: "sha256:" + strings.Repeat("a", 64),
		Capabilities: []string{"context.resources"},
	}}
	manifest.Digest = compiledManifestDigest(manifest.Files, manifest.Interfaces, manifest.Plugins, nil, nil, nil)
	writeCompiledManifest(t, path, manifest)
	if _, err := loadCompiledArtifact(directory, source); err != nil {
		t.Fatalf("valid plugin provenance was rejected: %v", err)
	}

	mutations := map[string]func(*CompiledPlugin){
		"version":    func(plugin *CompiledPlugin) { plugin.Version = "1.0.1" },
		"digest":     func(plugin *CompiledPlugin) { plugin.Digest = "sha256:" + strings.Repeat("b", 64) },
		"capability": func(plugin *CompiledPlugin) { plugin.Capabilities = []string{"context.session"} },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := manifest
			changed.Plugins = append([]CompiledPlugin(nil), manifest.Plugins...)
			mutate(&changed.Plugins[0])
			writeCompiledManifest(t, path, changed)
			if _, err := loadCompiledArtifact(directory, source); err == nil || !strings.Contains(err.Error(), "manifest digest") {
				t.Fatalf("got error %v, want plugin provenance digest rejection", err)
			}
		})
	}
}

func TestCompiledArtifactRejectsUnknownPluginCapability(t *testing.T) {
	source, directory := compiledServerArtifactFixture(t)
	path := filepath.Join(directory, compiledManifestFilename)
	manifest := readCompiledManifest(t, path)
	manifest.Plugins = []CompiledPlugin{{
		Name: "clock", Version: "1.0.0", Digest: "sha256:" + strings.Repeat("a", 64),
		Capabilities: []string{"system.root"},
	}}
	manifest.Digest = compiledManifestDigest(manifest.Files, manifest.Interfaces, manifest.Plugins, nil, nil, nil)
	writeCompiledManifest(t, path, manifest)
	if _, err := loadCompiledArtifact(directory, source); err == nil || !strings.Contains(err.Error(), "unknown capability") {
		t.Fatalf("got error %v, want unknown plugin capability rejection", err)
	}
}

func TestCompiledPluginDigestMatchesPythonCompilerContract(t *testing.T) {
	files := []CompiledFile{{Path: "agent.py", SHA256: strings.Repeat("a", 64), Size: 3}}
	plugins := []CompiledPlugin{{
		Name: "clock", Version: "1.2.3", Digest: "sha256:" + strings.Repeat("b", 64),
		Requires: []string{"core"}, Capabilities: []string{"context.resources", "lifecycle.tool"},
		Dependencies: []string{"httpx>=0.28,<1"},
	}}
	want := "sha256:876d5e9f129cfc2e952acbfae50253a382a3393f8235fc69e0a95035bfd98ea6"
	if got := compiledManifestDigest(files, CompiledInterfaces{}, plugins, nil, nil, nil); got != want {
		t.Fatalf("compiled manifest digest %q does not match Python contract %q", got, want)
	}
}

func TestCompiledArtifactValidatesCronRecords(t *testing.T) {
	source, directory := compiledCronArtifactFixture(t)
	path := filepath.Join(directory, compiledManifestFilename)
	baseline := readCompiledManifest(t, path)
	if _, err := loadCompiledArtifact(directory, source); err != nil {
		t.Fatalf("valid cron records were rejected: %v", err)
	}

	tests := map[string]struct {
		mutate  func(*CompiledManifest)
		message string
	}{
		"stable-name": {
			mutate:  func(manifest *CompiledManifest) { manifest.Crons[0].Name = "harnest.other.cron.alpha" },
			message: "stable source name",
		},
		"stable-source": {
			mutate:  func(manifest *CompiledManifest) { manifest.Crons[0].Source = "cron/renamed.py" },
			message: "stable source name",
		},
		"timezone": {
			mutate:  func(manifest *CompiledManifest) { manifest.Crons[0].Timezone = "Europe/London" },
			message: "timezone must be UTC",
		},
		"schedule": {
			mutate:  func(manifest *CompiledManifest) { manifest.Crons[0].Schedule = "0 9 * *" },
			message: "exactly five columns",
		},
		"task": {
			mutate:  func(manifest *CompiledManifest) { manifest.Crons[0].Task = "harnest.scheduler.tasks.missing" },
			message: "unknown task",
		},
		"duplicate": {
			mutate: func(manifest *CompiledManifest) {
				manifest.Crons = append(manifest.Crons, manifest.Crons[1])
			},
			message: "duplicate cron",
		},
		"order": {
			mutate: func(manifest *CompiledManifest) {
				manifest.Crons[0], manifest.Crons[1] = manifest.Crons[1], manifest.Crons[0]
			},
			message: "strictly sorted",
		},
		"manifest-bound-source": {
			mutate: func(manifest *CompiledManifest) {
				manifest.Crons[0].Name = "harnest.scheduler.cron.absent"
				manifest.Crons[0].Source = "cron/absent.py"
			},
			message: "source is not manifest-bound",
		},
	}
	for name, testCase := range tests {
		t.Run(name, func(t *testing.T) {
			changed := baseline
			changed.Crons = append([]CompiledCron(nil), baseline.Crons...)
			testCase.mutate(&changed)
			changed.Digest = compiledManifestDigest(
				changed.Files, changed.Interfaces, changed.Plugins, changed.Tasks, changed.Crons, changed.RuntimeDependencies,
			)
			writeCompiledManifest(t, path, changed)
			if _, err := loadCompiledArtifact(directory, source); err == nil || !strings.Contains(err.Error(), testCase.message) {
				t.Fatalf("got error %v, want %q", err, testCase.message)
			}
		})
	}
}

func TestCompiledCronScheduleMatchesNumericFiveColumnContract(t *testing.T) {
	for _, schedule := range []string{"0 9 * * *", "*/15 0,12 1-31/2 * 0-7"} {
		if err := validateCompiledCronSchedule(schedule); err != nil {
			t.Fatalf("valid schedule %q was rejected: %v", schedule, err)
		}
	}
	for _, schedule := range []string{"", " 0 9 * * *", "0 9 * *", "60 9 * * *", "0 9 2-1 * *", "0 9 * * */0", "٠ 9 * * *"} {
		if err := validateCompiledCronSchedule(schedule); err == nil {
			t.Fatalf("invalid schedule %q was accepted", schedule)
		}
	}
}

func TestCompiledCronDigestMatchesPythonCompilerContract(t *testing.T) {
	files := []CompiledFile{{Path: "agent.py", SHA256: strings.Repeat("a", 64), Size: 3}}
	tasks := []CompiledTask{{
		Name: "harnest.reporter.tasks.deliver", Source: "tasks/deliver.py",
		Queue: "reports", MaxRetries: 2,
	}}
	crons := []CompiledCron{{
		Name: "harnest.reporter.cron.daily_report", Source: "cron/daily_report.py",
		Schedule: "0 9 * * *", Timezone: "UTC", Task: tasks[0].Name,
	}}
	want := "sha256:390bf45c1b27717ca3422763fbc64da981d344ffe29513f26727f6745be1e499"
	if got := compiledManifestDigest(files, CompiledInterfaces{}, nil, tasks, crons, []string{procrastinateRuntimeRequirement}); got != want {
		t.Fatalf("compiled cron digest %q does not match Python contract %q", got, want)
	}
}

func TestDecodeCompiledManifestRejectsUnknownCronFields(t *testing.T) {
	input := strings.NewReader(`{"crons":[{"name":"schedule","unknown":true}]}`)
	if _, err := decodeCompiledManifest(input); err == nil || !strings.Contains(err.Error(), "unknown") {
		t.Fatalf("got error %v, want unknown cron field rejection", err)
	}
}

func readCompiledManifest(t *testing.T, path string) CompiledManifest {
	t.Helper()
	contents, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var manifest CompiledManifest
	if err := json.Unmarshal(contents, &manifest); err != nil {
		t.Fatal(err)
	}
	return manifest
}

// writeCompiledManifest changes only mutable manifest metadata in artifact tests.
func writeCompiledManifest(t *testing.T, path string, manifest CompiledManifest) {
	t.Helper()
	contents, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	mustWrite(t, path, string(contents))
}

func compiledServerArtifactFixture(t *testing.T) (Bundle, string) {
	t.Helper()
	source, err := LoadBundle(writeAgent(t, t.TempDir(), "serverpolicy", true))
	if err != nil {
		t.Fatal(err)
	}
	directory := filepath.Join(t.TempDir(), "artifact")
	copyTree(t, source.Directory, filepath.Join(directory, "source"))
	mustWrite(t, filepath.Join(directory, "__init__.py"), "from .agent import root_agent\n")
	mustWrite(t, filepath.Join(directory, "agent.py"), "root_agent = object()\n")
	mustWrite(t, filepath.Join(directory, compiledServerConfigFilename), "kind: Server\n")
	manifestJSON, err := json.Marshal(compiledManifestForDirectory(t, directory, source))
	if err != nil {
		t.Fatal(err)
	}
	mustWrite(t, filepath.Join(directory, compiledManifestFilename), string(manifestJSON))
	return source, directory
}

// compiledCronArtifactFixture creates an artifact whose schedules and task
// sources are all covered by the same immutable manifest file set.
func compiledCronArtifactFixture(t *testing.T) (Bundle, string) {
	t.Helper()
	root := writeAgent(t, t.TempDir(), "scheduler", true)
	mustWrite(t, filepath.Join(root, "tasks", "deliver.py"), "def deliver():\n    return None\n")
	mustWrite(t, filepath.Join(root, "cron", "alpha.py"), "alpha = object()\n")
	mustWrite(t, filepath.Join(root, "cron", "daily_report.py"), "daily_report = object()\n")
	source, err := LoadBundle(root)
	if err != nil {
		t.Fatal(err)
	}
	directory := filepath.Join(t.TempDir(), "artifact")
	copyTree(t, source.Directory, filepath.Join(directory, "source"))
	mustWrite(t, filepath.Join(directory, "__init__.py"), "from .agent import root_agent\n")
	mustWrite(t, filepath.Join(directory, "agent.py"), "root_agent = object()\n")
	mustWrite(t, filepath.Join(directory, compiledServerConfigFilename), "kind: Server\n")
	manifest := compiledManifestForDirectory(t, directory, source)
	task := CompiledTask{
		Name: "harnest.scheduler.tasks.deliver", Source: "tasks/deliver.py",
		Queue: "default", MaxRetries: 3,
	}
	manifest.Tasks = []CompiledTask{task}
	manifest.Crons = []CompiledCron{
		{Name: "harnest.scheduler.cron.alpha", Source: "cron/alpha.py", Schedule: "0 8 * * *", Timezone: "UTC", Task: task.Name},
		{Name: "harnest.scheduler.cron.daily_report", Source: "cron/daily_report.py", Schedule: "0 9 * * *", Timezone: "UTC", Task: task.Name},
	}
	manifest.RuntimeDependencies = []string{procrastinateRuntimeRequirement}
	manifest.Digest = compiledManifestDigest(
		manifest.Files, manifest.Interfaces, manifest.Plugins, manifest.Tasks, manifest.Crons, manifest.RuntimeDependencies,
	)
	writeCompiledManifest(t, filepath.Join(directory, compiledManifestFilename), manifest)
	return source, directory
}

func TestDigestSeparatesFileBoundaries(t *testing.T) {
	first := t.TempDir()
	second := t.TempDir()
	mustWrite(t, filepath.Join(first, "a"), "b\x00X")
	mustWrite(t, filepath.Join(second, "a"), "")
	mustWrite(t, filepath.Join(second, "b"), "X")
	firstDigest, err := digestDirectory(first)
	if err != nil {
		t.Fatal(err)
	}
	secondDigest, err := digestDirectory(second)
	if err != nil {
		t.Fatal(err)
	}
	if firstDigest == secondDigest {
		t.Fatalf("structurally different directories have the same digest %s", firstDigest)
	}
}

type recordingDeployer struct {
	mu    sync.Mutex
	names []string
}

type failingDeployer struct {
	mu    sync.Mutex
	names []string
}

type recordingCompiler struct {
	names []string
}

func (c *recordingCompiler) Compile(_ context.Context, bundle Bundle) (CompiledArtifact, error) {
	c.names = append(c.names, bundle.Config.Metadata.Name)
	return CompiledArtifact{
		Directory: "/compiled/" + bundle.Config.Metadata.Name,
		Manifest: CompiledManifest{
			Entrypoint: "agent:root_agent",
		},
	}, nil
}

type compiledRecordingDeployer struct {
	entrypoints []string
}

func (d *compiledRecordingDeployer) Deploy(_ context.Context, bundle Bundle) error {
	if bundle.Compiled == nil {
		return fmt.Errorf("compiled artifact is missing")
	}
	d.entrypoints = append(d.entrypoints, bundle.Compiled.Manifest.Entrypoint)
	return nil
}

func (d *failingDeployer) Deploy(_ context.Context, bundle Bundle) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.names = append(d.names, bundle.Config.Metadata.Name)
	return fmt.Errorf("test failure")
}

func (d *recordingDeployer) Deploy(_ context.Context, bundle Bundle) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.names = append(d.names, bundle.Config.Metadata.Name)
	return nil
}

func testPlan(project string) DeploymentPlan {
	return DeploymentPlan{
		APIVersion: APIVersion, Kind: "DeploymentPlan", ProjectRoot: project,
		Parallelism: 2, Sources: []AgentSource{{Root: "agents", Include: []string{"*"}}},
	}
}

func writeAgent(t *testing.T, project, name string, enabled bool) string {
	t.Helper()
	directory := filepath.Join(project, "agents", name)
	if err := os.MkdirAll(directory, 0o755); err != nil {
		t.Fatal(err)
	}
	mustWrite(t, filepath.Join(directory, "agent.py"), "root_agent = object()\n")
	mustWrite(t, filepath.Join(directory, "instructions.md"), "Be useful.\n")
	mustWrite(t, filepath.Join(directory, "pyproject.toml"), `[project]
name = "test-agent"
version = "0.1.0"
dependencies = []
`)
	mustWrite(t, filepath.Join(directory, "config.yaml"), fmt.Sprintf(`apiVersion: harnest.dev/v1alpha1
kind: Agent
metadata:
  name: %s
spec:
  enabled: %t
  entrypoint: agent:root_agent
  framework:
    name: adk
    mode: managed
  runtime:
    version: "3.12"
    dependencyFile: pyproject.toml
  resources:
    cpu: "1"
    memory: 1Gi
`, name, enabled))
	mustWrite(t, filepath.Join(directory, "agent-card.yaml"), fmt.Sprintf(`name: %s
description: Test agent.
version: 1.0.0
supportedInterfaces:
  - url: https://example.com/a2a
    protocolBinding: JSONRPC
    protocolVersion: "1.0"
capabilities:
  streaming: true
defaultInputModes: [text/plain]
defaultOutputModes: [text/plain]
skills:
  - id: test
    name: Test
    description: Test things.
    tags: [test]
`, name))
	return directory
}

func stringPointer(value string) *string { return &value }

func copyTree(t *testing.T, source, destination string) {
	t.Helper()
	if err := filepath.WalkDir(source, func(filePath string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(source, filePath)
		if err != nil {
			return err
		}
		target := filepath.Join(destination, relative)
		if entry.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		content, err := os.ReadFile(filePath)
		if err != nil {
			return err
		}
		return os.WriteFile(target, content, 0o644)
	}); err != nil {
		t.Fatal(err)
	}
}

func compiledManifestForDirectory(t *testing.T, directory string, source Bundle) CompiledManifest {
	t.Helper()
	var files []CompiledFile
	if err := filepath.WalkDir(directory, func(filePath string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		relative, err := filepath.Rel(directory, filePath)
		if err != nil {
			return err
		}
		relative = filepath.ToSlash(relative)
		if relative == compiledServerConfigFilename {
			return nil
		}
		digest, size, err := hashFile(filePath)
		if err != nil {
			return err
		}
		files = append(files, CompiledFile{Path: relative, SHA256: digest, Size: size})
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	sort.Slice(files, func(left, right int) bool { return files[left].Path < files[right].Path })
	return CompiledManifest{
		APIVersion: APIVersion, Kind: "CompiledAgent", Name: source.Config.Metadata.Name,
		Entrypoint: "agent:root_agent", SourceEntrypoint: source.Config.Spec.Entrypoint,
		SourceDirectory: "source", HarnestVersion: "0.1.0",
		Framework: compiledFrameworkForTest(source.Config.Spec.Framework),
		Checkpoint: CompiledCheckpoint{
			Owner: "harnest", Framework: "portable", Schema: "harnest-checkpoint/v1",
		},
		Interfaces: CompiledInterfaces{CLI: source.Config.Spec.Interfaces.CLI},
		Digest:     compiledManifestDigest(files, CompiledInterfaces{CLI: source.Config.Spec.Interfaces.CLI}, nil, nil, nil, nil), Files: files,
	}
}

func compiledFrameworkForTest(framework AgentFramework) CompiledFramework {
	distribution := map[string]string{"adk": "google-adk", "langgraph": "langgraph"}[framework.Name]
	return CompiledFramework{
		Name: framework.Name, Mode: framework.EffectiveMode(),
		Distribution: distribution, Version: "1.2.0",
	}
}

func mustWrite(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
