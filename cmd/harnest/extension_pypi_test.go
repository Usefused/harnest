package main

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestExtensionInstallFromPyPIPinsVerifiedReleaseWithoutImporting(t *testing.T) {
	wheel := extensionWheelFixture(
		t, "harnest-extension-docker", "docker", "harnest_extension_docker", "0.2.0",
	)
	requests := 0
	sys := pluginSearchTestSystem(extensionInstallTransport(t, wheel, &requests), t.TempDir())
	project := extensionTestProject(t)
	stdout, _, err := executeForTest(
		t, sys, "extensions", "install", "docker", "--project", project,
	)
	if err != nil {
		t.Fatal(err)
	}
	destination := filepath.Join(project, "extensions", "docker")
	assertContainsAll(t, "PyPI extension install", stdout, []string{destination, "harnest env sync"})
	assertFilesContain(t, destination, map[string]string{
		"README.md":      "# Docker extension",
		"extension.yaml": "version: 0.2.0",
		"extension.py":   "installer executed package code",
		"pyproject.toml": `name = 'harnest-extension-docker'`,
	})
	if project := string(mustReadTestFile(t, filepath.Join(destination, "pyproject.toml"))); !strings.Contains(project, "readme = 'README.md'") {
		t.Fatalf("installed project does not retain README metadata:\n%s", project)
	}
	if requests != 2 {
		t.Fatalf("PyPI install requests = %d, want metadata plus wheel", requests)
	}
	values, _, err := pluginRuntimeRequirements(project)
	if err != nil || fmt.Sprint(values) != "[docker>=7.1,<8 harnest>=0.14,<0.15]" {
		t.Fatalf("installed runtime dependencies = %v, %v", values, err)
	}
}

func TestExtensionInstallFromPyPIRejectsArtifactMismatchBeforeMutation(t *testing.T) {
	wheel := extensionWheelFixture(
		t, "harnest-extension-docker", "docker", "harnest_extension_docker", "0.2.0",
	)
	transport := extensionInstallTransport(t, wheel, nil)
	sys := pluginSearchTestSystem(pluginRoundTripFunc(func(request *http.Request) (*http.Response, error) {
		response, err := transport.RoundTrip(request)
		if strings.HasPrefix(request.URL.Path, "/files/") {
			return pluginHTTPBytesResponse(http.StatusOK, append(wheel, 'x'), nil), nil
		}
		return response, err
	}), t.TempDir())
	project := extensionTestProject(t)
	_, _, err := executeForTest(
		t, sys, "extensions", "install", "harnest-extension-docker", "--project", project,
	)
	if err == nil || !strings.Contains(err.Error(), "does not match PyPI metadata") {
		t.Fatalf("artifact mismatch error = %v", err)
	}
	assertExtensionDirectoryEmpty(t, filepath.Join(project, "extensions"))
}

func TestExtensionInstallFromPyPIRejectsCasefoldResourceCollision(t *testing.T) {
	wheel := extensionWheelFixtureWithFiles(
		t, "harnest-extension-docker", "docker", "harnest_extension_docker", "0.2.0",
		"extension = object()\n",
		map[string]string{"lib/A.py": "first\n", "lib/a.py": "second\n"},
	)
	sys := pluginSearchTestSystem(extensionInstallTransport(t, wheel, nil), t.TempDir())
	project := extensionTestProject(t)
	_, _, err := executeForTest(
		t, sys, "extensions", "install", "docker", "--project", project,
	)
	if err == nil || !strings.Contains(err.Error(), "conflict by case") {
		t.Fatalf("case-fold collision error = %v", err)
	}
	assertExtensionDirectoryEmpty(t, filepath.Join(project, "extensions"))
}

func TestExtensionWheelAllowsOnlyRegularRootReadme(t *testing.T) {
	for _, test := range []struct {
		name     string
		resource string
		mode     os.FileMode
	}{
		{name: "symlink", resource: "README.md", mode: os.ModeSymlink | 0o777},
		{name: "special", resource: "README.md", mode: os.ModeNamedPipe | 0o644},
		{name: "unexpected", resource: "NOTES.md", mode: 0o644},
	} {
		t.Run(test.name, func(t *testing.T) {
			var buffer bytes.Buffer
			writer := zip.NewWriter(&buffer)
			header := &zip.FileHeader{Name: "harnest_extension_docker/" + test.resource}
			header.SetMode(test.mode)
			file, err := writer.CreateHeader(header)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := file.Write([]byte("resource\n")); err != nil {
				t.Fatal(err)
			}
			if err := writer.Close(); err != nil {
				t.Fatal(err)
			}
			reader, err := zip.NewReader(bytes.NewReader(buffer.Bytes()), int64(buffer.Len()))
			if err != nil {
				t.Fatal(err)
			}
			if err := validateExtensionWheelResource(test.resource, reader.File[0]); err == nil {
				t.Fatalf("%s wheel README was accepted", test.name)
			}
		})
	}
}

func TestExtensionWheelMetadataRejectsCaseInsensitiveDuplicate(t *testing.T) {
	var buffer bytes.Buffer
	writer := zip.NewWriter(&buffer)
	for _, name := range []string{"first.dist-info/METADATA", "second.dist-info/metadata"} {
		if _, err := writer.Create(name); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	reader, err := zip.NewReader(bytes.NewReader(buffer.Bytes()), int64(buffer.Len()))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := findExtensionWheelMetadata(reader.File); err == nil ||
		!strings.Contains(err.Error(), "ambiguous") {
		t.Fatalf("duplicate METADATA error = %v", err)
	}
}

func TestExtensionInstallFromPyPIActivatesMaterializedLocalClass(t *testing.T) {
	source := `from harnest.extensions import Extension

class DockerExtension(Extension):
    pass

extension = DockerExtension()
docker = extension
`
	wheel := extensionWheelFixtureWithSource(
		t, "harnest-extension-docker", "docker", "harnest_extension_docker", "0.2.0", source,
	)
	sys := pluginSearchTestSystem(extensionInstallTransport(t, wheel, nil), t.TempDir())
	project := extensionTestProject(t)
	if _, _, err := executeForTest(
		t, sys, "extensions", "install", "docker", "--project", project,
	); err != nil {
		t.Fatal(err)
	}
	repository, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	program := `import sys
from pathlib import Path
from harnest.plugins import activate_runtime_plugins, release_runtime_plugins
from harnest.runtime_plugins import discover_application_extensions
root = Path(sys.argv[1])
descriptors = discover_application_extensions(root)
activated = activate_runtime_plugins(descriptors)
try:
    plugin = activated[0].plugin
    assert type(plugin).__module__ == "harnest.extensions.docker"
    assert activated[0].module.docker is plugin
finally:
    release_runtime_plugins(descriptors)
`
	python := filepath.Join(repository, ".venv", "bin", "python")
	if _, err := os.Stat(python); err != nil {
		python, err = exec.LookPath("python3")
		if err != nil {
			t.Skip("Python is unavailable for extension activation")
		}
	}
	command := exec.Command(python, "-c", program, project)
	command.Env = append(os.Environ(), "PYTHONPATH="+filepath.Join(repository, "src"))
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("activate installed PyPI extension: %v\n%s", err, output)
	}
}

func TestExtensionInstallCanonicalPyPIProjects(t *testing.T) {
	for _, source := range []string{"docker", "harnest-extension-docker", "Harnest_Extension_Docker"} {
		name, err := canonicalExtensionProject(source)
		if err != nil || name != "harnest-extension-docker" {
			t.Errorf("canonicalExtensionProject(%q) = %q, %v", source, name, err)
		}
	}
}

func TestExtensionInstallRejectsAmbiguousSources(t *testing.T) {
	for _, source := range []string{"./docker", "missing/docker", "harnest-plugin-docker", "bad name"} {
		if source == "./docker" || source == "missing/docker" {
			if !localExtensionInstallSource(source) {
				t.Errorf("missing path %q was not retained as a local source", source)
			}
			continue
		}
		if _, err := canonicalExtensionProject(source); err == nil {
			t.Errorf("invalid canonical source %q was accepted", source)
		}
	}
}

func TestExtensionInstallRecognizesOfficialProjects(t *testing.T) {
	for _, project := range []string{"harnest-extension-docker", "Harnest_Extension_Hatchet"} {
		if trust := pluginProjectTrust(project); trust != "official" {
			t.Errorf("trust for %s = %s", project, trust)
		}
	}
}

// extensionInstallTransport serves one immutable release through PyPI's two endpoints.
func extensionInstallTransport(
	t *testing.T, wheel []byte, requests *int,
) http.RoundTripper {
	t.Helper()
	project, release := "harnest-extension-docker", "0.2.0"
	return pluginRoundTripFunc(func(request *http.Request) (*http.Response, error) {
		if requests != nil {
			*requests++
		}
		switch request.URL.Path {
		case "/pypi/harnest-extension-docker/json":
			return pluginHTTPResponse(
				http.StatusOK, pluginMetadataFixture(t, project, wheel, release), nil,
			), nil
		case "/files/harnest-extension-docker.whl":
			return pluginHTTPBytesResponse(http.StatusOK, wheel, nil), nil
		default:
			return pluginHTTPResponse(http.StatusNotFound, "", nil), nil
		}
	})
}

// extensionWheelFixture authors a canonical wheel whose Python must never run in the CLI.
func extensionWheelFixture(
	t *testing.T, project, entryName, module, release string,
) []byte {
	return extensionWheelFixtureWithSource(
		t, project, entryName, module, release,
		"raise AssertionError('installer executed package code')\n",
	)
}

func extensionWheelFixtureWithSource(
	t *testing.T, project, entryName, module, release, source string,
) []byte {
	return extensionWheelFixtureWithFiles(
		t, project, entryName, module, release, source, nil,
	)
}

func extensionWheelFixtureWithFiles(
	t *testing.T, project, entryName, module, release, source string,
	extra map[string]string,
) []byte {
	t.Helper()
	var buffer bytes.Buffer
	writer := zip.NewWriter(&buffer)
	distInfo := strings.ReplaceAll(project, "-", "_") + "-" + release + ".dist-info"
	files := map[string]string{
		distInfo + "/entry_points.txt": fmt.Sprintf(
			"[harnest.extensions]\n%s = %s.extension:extension\n", entryName, module,
		),
		strings.ReplaceAll(module, ".", "/") + "/extension.yaml": fmt.Sprintf(
			"apiVersion: harnest.dev/v1alpha1\nkind: Extension\nmetadata:\n  name: %s\n  version: %s\nruntime:\n  entrypoint: extension:extension\ncapabilities: [sandbox.provider]\n",
			entryName, release,
		),
		strings.ReplaceAll(module, ".", "/") + "/extension.py": source,
		strings.ReplaceAll(module, ".", "/") + "/README.md":    "# Docker extension\n",
		distInfo + "/METADATA": "Metadata-Version: 2.4\n" +
			"Name: " + project + "\n" +
			"Version: " + release + "\n" +
			"Requires-Python: >=3.10\n" +
			"Requires-Dist: docker>=7.1,<8\n" +
			"Requires-Dist: harnest>=0.14,<0.15\n\n",
	}
	for name, contents := range extra {
		files[strings.ReplaceAll(module, ".", "/")+"/"+name] = contents
	}
	for name, contents := range files {
		file, err := writer.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := file.Write([]byte(contents)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return buffer.Bytes()
}

func TestPyPIExtensionProjectIsValidTOML(t *testing.T) {
	root := t.TempDir()
	downloaded := pypiExtensionPackage{
		ProjectName: "harnest-extension-docker", Version: "0.2.0",
		RequiresPython: ">=3.10", Dependencies: []string{"docker>=7.1,<8"},
		Resources: map[string][]byte{"README.md": []byte("# Docker extension\n")},
	}
	contents, err := pypiExtensionProjectSource(downloaded)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(
		filepath.Join(root, "pyproject.toml"),
		contents,
		0o644,
	); err != nil {
		t.Fatal(err)
	}
	project, err := readLocalExtensionProject(filepath.Join(root, "pyproject.toml"))
	if err != nil || project["name"] != "harnest-extension-docker" {
		t.Fatalf("generated loader project = %#v, %v", project, err)
	}
	if project["readme"] != "README.md" {
		t.Fatalf("generated loader README = %#v", project["readme"])
	}
	dependencies, ok := project["dependencies"].([]any)
	if !ok || fmt.Sprint(dependencies) != "[docker>=7.1,<8]" {
		t.Fatalf("generated loader dependencies = %#v", project["dependencies"])
	}
}

func TestExtensionMetadataFixtureRemainsJSON(t *testing.T) {
	// Keep the shared transport helper's serialized shape independently readable.
	var metadata pypiProjectMetadata
	wheel := extensionWheelFixture(t, "harnest-extension-docker", "docker", "harnest_extension_docker", "0.2.0")
	if err := json.Unmarshal([]byte(pluginMetadataFixture(t, "harnest-extension-docker", wheel, "0.2.0")), &metadata); err != nil {
		t.Fatal(err)
	}
}
