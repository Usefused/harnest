package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
)

const testTemplateModule = "harnest_template_support"
const testTemplateDistInfo = "harnest_template_support-0.1.0.dist-info"

func defaultTemplateEntries() map[string][]byte {
	return map[string][]byte{
		testTemplateModule + "/harnest-template.yaml": []byte(
			"apiVersion: harnest.dev/v1alpha1\nkind: Template\nmetadata:\n  name: support\n",
		),
		testTemplateModule + "/template/config.yaml": []byte(
			"apiVersion: harnest.dev/v1alpha1\nkind: Agent\nmetadata:\n  name: {{ .Name }}\n  displayName: {{ .DisplayName }}\nspec:\n  entrypoint: agent:root_agent\n  framework:\n    name: adk\n    mode: managed\n  runtime:\n    version: \"3.12\"\n    dependencyFile: pyproject.toml\n",
		),
		testTemplateModule + "/template/agent.py": []byte(
			"from harnest.agent import Agent\n\nroot_agent = Agent(name=\"{{ .AdkName }}\")\n",
		),
		testTemplateModule + "/template/pyproject.toml": []byte(
			"[project]\nname = \"{{ .Name }}\"\n",
		),
		testTemplateDistInfo + "/METADATA": []byte(
			"Metadata-Version: 2.1\nName: harnest-template-support\nVersion: 0.1.0\n",
		),
		testTemplateDistInfo + "/WHEEL": []byte(
			"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
		),
		testTemplateDistInfo + "/entry_points.txt": []byte(
			"[harnest.templates]\nsupport = harnest_template_support.template:template\n",
		),
	}
}

func buildTemplateWheel(t *testing.T, entries map[string][]byte, symlinks map[string]string) []byte {
	t.Helper()
	var buffer bytes.Buffer
	writer := zip.NewWriter(&buffer)
	names := make([]string, 0, len(entries))
	for name := range entries {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		file, err := writer.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := file.Write(entries[name]); err != nil {
			t.Fatal(err)
		}
	}
	for name, target := range symlinks {
		header := &zip.FileHeader{Name: name, Method: zip.Deflate}
		header.SetMode(os.ModeSymlink | 0o777)
		file, err := writer.CreateHeader(header)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := file.Write([]byte(target)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return buffer.Bytes()
}

func defaultTemplateWheel(t *testing.T) []byte {
	t.Helper()
	return buildTemplateWheel(t, defaultTemplateEntries(), nil)
}

func templateWheelServer(t *testing.T, wheel []byte) *httptest.Server {
	t.Helper()
	server := httptest.NewTLSServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		if _, err := response.Write(wheel); err != nil {
			t.Error(err)
		}
	}))
	t.Cleanup(server.Close)
	return server
}

func testTemplateApplication() *application {
	app := &application{system: defaultSystem(), version: "test"}
	app.system.httpClient = &http.Client{
		Timeout:   30 * time.Second,
		Transport: &http.Transport{TLSClientConfig: &tls.Config{InsecureSkipVerify: true}},
	}
	return app
}

func TestCreateScaffoldFromTemplateWritesInertAgentTree(t *testing.T) {
	server := templateWheelServer(t, defaultTemplateWheel(t))
	app := testTemplateApplication()
	target := filepath.Join(t.TempDir(), "my-agent")

	framework, err := app.createScaffoldFromTemplate(
		context.Background(), target, "my-agent", server.URL+"/support.whl", "",
	)
	if err != nil {
		t.Fatal(err)
	}
	if framework != "adk" {
		t.Fatalf("framework = %q, want adk", framework)
	}
	assertScaffoldSubstitution(t, target)
	assertNoPackagingLeaks(t, target)
}

func assertScaffoldSubstitution(t *testing.T, target string) {
	t.Helper()
	config, err := os.ReadFile(filepath.Join(target, "config.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{"name: my-agent", "displayName: My Agent"} {
		if !strings.Contains(string(config), expected) {
			t.Fatalf("config.yaml missing %q:\n%s", expected, config)
		}
	}
	if strings.Contains(string(config), "{{") {
		t.Fatalf("config.yaml retained an unsubstituted placeholder:\n%s", config)
	}
	agent, err := os.ReadFile(filepath.Join(target, "agent.py"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(agent), `name="my_agent"`) {
		t.Fatalf("agent.py missing substituted identifier:\n%s", agent)
	}
}

func assertNoPackagingLeaks(t *testing.T, target string) {
	t.Helper()
	for _, forbidden := range []string{"harnest-template.yaml", "template", ".dist-info"} {
		if _, err := os.Lstat(filepath.Join(target, forbidden)); !os.IsNotExist(err) {
			t.Fatalf("target contains packaging path %s", forbidden)
		}
	}
}

func TestCreateScaffoldFromTemplateRejectsUnsafeResourcePath(t *testing.T) {
	entries := defaultTemplateEntries()
	entries[testTemplateModule+"/template/../evil.py"] = []byte("bad")
	server := templateWheelServer(t, buildTemplateWheel(t, entries, nil))
	app := testTemplateApplication()
	target := filepath.Join(t.TempDir(), "agent")

	if _, err := app.createScaffoldFromTemplate(
		context.Background(), target, "agent", server.URL+"/unsafe.whl", "",
	); err == nil {
		t.Fatal("unsafe resource path was accepted")
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Fatalf("target was created despite unsafe archive")
	}
}

func TestCreateScaffoldFromTemplateRejectsDigestMismatch(t *testing.T) {
	server := templateWheelServer(t, defaultTemplateWheel(t))
	app := testTemplateApplication()
	target := filepath.Join(t.TempDir(), "agent")

	_, err := app.createScaffoldFromTemplate(
		context.Background(), target, "agent", server.URL+"/support.whl",
		strings.Repeat("0", 64),
	)
	if err == nil || !strings.Contains(err.Error(), "sha256") {
		t.Fatalf("digest mismatch error = %v", err)
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Fatalf("target was created despite digest mismatch")
	}
}

func TestCreateScaffoldFromTemplateRejectsNonAgentTemplate(t *testing.T) {
	entries := defaultTemplateEntries()
	delete(entries, testTemplateModule+"/template/agent.py")
	server := templateWheelServer(t, buildTemplateWheel(t, entries, nil))
	app := testTemplateApplication()
	target := filepath.Join(t.TempDir(), "agent")

	_, err := app.createScaffoldFromTemplate(
		context.Background(), target, "agent", server.URL+"/incomplete.whl", "",
	)
	if err == nil || !strings.Contains(err.Error(), "agent.py") {
		t.Fatalf("non-agent template error = %v", err)
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Fatalf("target was left behind after failed initialization")
	}
}

func TestReadTemplateWheelPackageRejectsSymlink(t *testing.T) {
	wheel := buildTemplateWheel(t, defaultTemplateEntries(), map[string]string{
		testTemplateModule + "/template/link.py": "agent.py",
	})
	if _, err := readTemplateWheelPackage(wheel); err == nil ||
		!strings.Contains(err.Error(), "regular files") {
		t.Fatalf("symlink resource error = %v", err)
	}
}

func TestReadTemplateWheelPackageRejectsMismatchedEntryPoint(t *testing.T) {
	entries := defaultTemplateEntries()
	entries[testTemplateDistInfo+"/entry_points.txt"] = []byte(
		"[harnest.templates]\nother = harnest_template_support.template:template\n",
	)
	wheel := buildTemplateWheel(t, entries, nil)
	if _, err := readTemplateWheelPackage(wheel); err == nil ||
		!strings.Contains(err.Error(), "entry point") {
		t.Fatalf("mismatched entry point error = %v", err)
	}
}

func TestCanonicalTemplateProject(t *testing.T) {
	name, err := canonicalTemplateProject("support")
	if err != nil || name != "harnest-template-support" {
		t.Fatalf("canonicalTemplateProject(support) = %q, %v", name, err)
	}
	name, err = canonicalTemplateProject("harnest-template-support-agent")
	if err != nil || name != "harnest-template-support-agent" {
		t.Fatalf("canonicalTemplateProject(prefixed) = %q, %v", name, err)
	}
	if _, err := canonicalTemplateProject("not/a project"); err == nil {
		t.Fatal("invalid template project was accepted")
	}
}

func TestSelectTemplateWheelRequiresUniversalWheel(t *testing.T) {
	wheel := defaultTemplateWheel(t)
	digest := sha256.Sum256(wheel)
	universal := pypiReleaseFile{
		Filename:    "harnest_template_support-0.1.0-py3-none-any.whl",
		PackageType: "bdist_wheel", Size: int64(len(wheel)),
	}
	universal.Digests.SHA256 = hex.EncodeToString(digest[:])
	platform := universal
	platform.Filename = "harnest_template_support-0.1.0-cp313-cp313-win_amd64.whl"

	selected, err := selectTemplateWheel([]pypiReleaseFile{platform, universal})
	if err != nil {
		t.Fatal(err)
	}
	if selected.Filename != universal.Filename {
		t.Fatalf("selected wheel = %q, want universal wheel", selected.Filename)
	}
	if _, err := selectTemplateWheel([]pypiReleaseFile{platform}); err == nil {
		t.Fatal("platform-only wheel was accepted")
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

func TestDownloadTemplateReferenceResolvesPyPIProject(t *testing.T) {
	wheel := defaultTemplateWheel(t)
	digest := sha256.Sum256(wheel)
	metadata := pypiProjectMetadata{}
	metadata.Info.Name = "harnest-template-support"
	metadata.Info.Version = "0.1.0"
	metadata.URLs = []pypiReleaseFile{{
		Filename:    "harnest_template_support-0.1.0-py3-none-any.whl",
		PackageType: "bdist_wheel",
		URL:         "https://files.pythonhosted.org/packages/support/harnest_template_support-0.1.0-py3-none-any.whl",
		Size:        int64(len(wheel)),
	}}
	metadata.URLs[0].Digests.SHA256 = hex.EncodeToString(digest[:])
	payload, err := json.Marshal(metadata)
	if err != nil {
		t.Fatal(err)
	}

	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.URL.Path == "/pypi/harnest-template-support/json" {
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(bytes.NewReader(payload)),
				Request:    request,
			}, nil
		}
		if request.URL.Host == "files.pythonhosted.org" {
			return &http.Response{
				StatusCode:    http.StatusOK,
				Body:          io.NopCloser(bytes.NewReader(wheel)),
				ContentLength: int64(len(wheel)),
				Request:       request,
			}, nil
		}
		return &http.Response{
			StatusCode: http.StatusNotFound,
			Body:       io.NopCloser(strings.NewReader("not found")),
			Request:    request,
		}, nil
	})}

	app := testTemplateApplication()
	app.system.httpClient = client

	contents, projectName, release, err := app.downloadTemplateReference(
		context.Background(), "support", "",
	)
	if err != nil {
		t.Fatal(err)
	}
	if projectName != "harnest-template-support" || release != "0.1.0" {
		t.Fatalf("resolved identity = %q %q", projectName, release)
	}
	pkg, err := readTemplateWheelPackage(contents)
	if err != nil {
		t.Fatal(err)
	}
	if pkg.Slug != "support" || len(pkg.Resources) == 0 {
		t.Fatalf("parsed template = %+v", pkg)
	}
}

func TestValidateTemplateFlagsRejectsFrameworkOverride(t *testing.T) {
	app := testTemplateApplication()
	command := app.newInitCommand()
	if err := command.Flags().Set("framework", "langgraph"); err != nil {
		t.Fatal(err)
	}
	if err := validateTemplateFlags(command); err == nil ||
		!strings.Contains(err.Error(), "framework") {
		t.Fatalf("validateTemplateFlags = %v", err)
	}
}
