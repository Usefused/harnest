package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"harnest.dev/harnest/internal/agentpack"
	"harnest.dev/harnest/internal/runtimewheel"
)

// selectedCompileFixture includes an impossible unselected integration to expose accidental all-extras resolution.
func selectedCompileFixture(t *testing.T) string {
	t.Helper()
	root := packProject(t, "sales", `"httpx>=0.28"`)
	file, err := os.OpenFile(filepath.Join(root, "pyproject.toml"), os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	_, err = file.WriteString("\n[project.optional-dependencies]\ncrm=['company-sdk==2.0']\nunused=['never-install-unused==1.0']\n")
	if err != nil {
		t.Fatal(err)
	}
	mustWriteEnvironmentFixture(t, filepath.Join(root, compileSelectionFile), "version: 1\nextras: [crm, crm]\n")
	return root
}

// TestCompileSelectionIsolatedAndDeduplicated proves only compilation adds the selected optional roots.
func TestCompileSelectionIsolatedAndDeduplicated(t *testing.T) {
	root := selectedCompileFixture(t)
	bundle, err := loadAgentBundle(root)
	if err != nil {
		t.Fatal(err)
	}
	ordinary, err := inspectProfileDependencyPlan(bundle, runtimeEnvironmentProfile)
	if err != nil || len(ordinary.CompileRequirements) != 0 {
		t.Fatalf("ordinary run changed: %#v %v", ordinary, err)
	}
	plan, err := sharedRuntimePlan([]string{root, root})
	if err != nil {
		t.Fatal(err)
	}
	input, err := writePackRequirements(t.TempDir(), "/wheel.whl", plan)
	if err != nil {
		t.Fatal(err)
	}
	body := string(mustReadTestFile(t, input))
	if strings.Count(body, "company-sdk==2.0") != 1 || strings.Contains(body, "never-install-unused") {
		t.Fatalf("bad selection: %s", body)
	}
	if len(plan.Inputs) != 1 {
		t.Fatalf("equivalent owners not deduplicated: %v", plan.Inputs)
	}
	mustWriteEnvironmentFixture(t, filepath.Join(root, compileSelectionFile), "version: 1\nextras: [unused]\n")
	if err = validatePackAgent(bundle, agentpack.Manifest{Inputs: plan.Inputs}); err == nil {
		t.Fatal("changed selected requirements reused runtime")
	}
}

// TestCompileSelectionInvalidatesOnlyCompileEnvironment binds selected roots to the cached environment and lock.
func TestCompileSelectionInvalidatesOnlyCompileEnvironment(t *testing.T) {
	root := selectedCompileFixture(t)
	bundle, err := loadAgentBundle(root)
	if err != nil {
		t.Fatal(err)
	}
	plan, err := inspectCompileDependencyPlan(bundle)
	if err != nil {
		t.Fatal(err)
	}
	wheel := runtimewheel.Artifact{Name: "harnest.whl", Contents: []byte("wheel")}
	before, err := environmentFingerprint(bundle, wheel, plan, compileEnvironmentProfile)
	if err != nil {
		t.Fatal(err)
	}
	lockBefore, err := runtimeLockInputFingerprint(bundle, wheel, plan, compileEnvironmentProfile)
	if err != nil {
		t.Fatal(err)
	}
	plan.CompileRequirements = []string{"other==1"}
	after, err := environmentFingerprint(bundle, wheel, plan, compileEnvironmentProfile)
	if err != nil {
		t.Fatal(err)
	}
	lockAfter, err := runtimeLockInputFingerprint(bundle, wheel, plan, compileEnvironmentProfile)
	if err != nil || before == after || lockBefore == lockAfter {
		t.Fatalf("selected requirements did not invalidate fingerprints: %v", err)
	}
}

// TestCompileManifestRejectsAmbiguity fails unknown fields, bad types, missing extras, and escaping paths.
func TestCompileManifestRejectsAmbiguity(t *testing.T) {
	for _, body := range []string{"version: 1\nresources: [instructions.md]", "version: 1\nresources: [skills/help]", "version: 2", "version: true", "version: 1\nextra: [crm]", "version: 1\nextras: null", "version: 1\nextras: [1]", "version: 1\nresources: [../secret]", "version: 1\nresources: ['C:/secret']", "version: 1\nversion: 1", "version: 1\n---\nversion: 1", "version: 1\nextras: [missing]"} {
		t.Run(body, func(t *testing.T) {
			root := selectedCompileFixture(t)
			mustWriteEnvironmentFixture(t, filepath.Join(root, compileSelectionFile), body)
			if _, err := sharedRuntimePlan([]string{root}); err == nil {
				t.Fatal("invalid selection accepted")
			}
		})
	}
}

// TestRuntimeReportAccountsForWholePackages keeps resources and modules in package size accounting.
func TestRuntimeReportAccountsForWholePackages(t *testing.T) {
	root := t.TempDir()
	metadata := filepath.Join(root, "demo-1.0.dist-info")
	if err := os.MkdirAll(metadata, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "demo"), 0755); err != nil {
		t.Fatal(err)
	}
	mustWriteEnvironmentFixture(t, filepath.Join(metadata, "METADATA"), "Name: demo\nVersion: 1.0\nRequires-Dist: helper>=1\n\n")
	mustWriteEnvironmentFixture(t, filepath.Join(root, "demo", "unused.py"), "unimported module")
	mustWriteEnvironmentFixture(t, filepath.Join(root, "demo", "data.json"), "{}")
	mustWriteEnvironmentFixture(t, filepath.Join(metadata, "RECORD"), "demo/unused.py,,\ndemo/data.json,,\n../../outside,,\n")
	items, err := installedPackDistributions(root, []packRequirementReason{{"demo==1.0", "sales/pyproject.toml"}})
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items[0].Size != 19 || len(items[0].Requires) != 1 || !strings.Contains(items[0].Reasons[0], "sales") {
		t.Fatalf("bad inventory: %#v", items)
	}
}

// TestRuntimeReportUniqueObjectBytes distinguishes repeated paths from physical content storage.
func TestRuntimeReportUniqueObjectBytes(t *testing.T) {
	stage := t.TempDir()
	files := []agentpack.File{{Path: "a", Object: "same", Size: 12}, {Path: "b", Object: "same", Size: 12}}
	report, err := makeRuntimeBuildReport(stage, runtimePackPlan{}, files)
	if err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	if report.LogicalBytes != 24 || report.UniqueObjectBytes != 12 {
		t.Fatalf("bad dedup accounting: %s", data)
	}
}

// TestCompileSelectionReachesResolver verifies the public compile command uses its own synchronized environment.
func TestCompileSelectionReachesResolver(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("shell resolver fixture; Windows exercises the real resolver in native smoke CI")
	}
	root := selectedCompileFixture(t)
	calls := filepath.Join(t.TempDir(), "calls.txt")
	t.Setenv("HARNEST_ENV_TEST_CALLS", calls)
	if _, _, err := executeForTest(t, environmentTestSystem(t, t.TempDir()), "compile", root, "--output", filepath.Join(t.TempDir(), "artifact")); err != nil {
		t.Fatal(err)
	}
	body := string(mustReadTestFile(t, calls))
	if !strings.Contains(body, "company-sdk==2.0") || strings.Contains(body, "never-install-unused") {
		t.Fatalf("wrong resolver input: %s", body)
	}
	assertFilesExist(t, root, []string{compileEnvironmentProfile.requirementsLockFile(), filepath.Join(".harnest", compileEnvironmentProfile.stateFile())})
	if _, err := os.Stat(filepath.Join(root, runtimeRequirementsLockFile)); !os.IsNotExist(err) {
		t.Fatalf("compile changed regular runtime lock: %v", err)
	}
}

// TestCompileRequirementsRejectResolverDirectives keeps selected metadata from adding installer options.
func TestCompileRequirementsRejectResolverDirectives(t *testing.T) {
	for _, value := range []string{"--index-url https://invalid.example", "demo==1\n--no-deps", "-r other.txt"} {
		if err := validateCompileRequirements([]string{value}); err == nil {
			t.Fatalf("accepted resolver directive: %q", value)
		}
	}
}
