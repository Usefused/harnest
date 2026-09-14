package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type extensionRoundTripFunc func(*http.Request) (*http.Response, error)

func (function extensionRoundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func TestExtensionsSearchFiltersPyPIAndReusesFreshCatalog(t *testing.T) {
	var catalogRequests int
	var metadataRequests int
	var wheelRequests int
	transport := extensionCatalogFixture(
		t, &catalogRequests, &metadataRequests, &wheelRequests,
	)

	cacheRoot := t.TempDir()
	sys := extensionSearchTestSystem(transport, cacheRoot)
	stdout, _, err := executeForTest(t, sys, "extensions", "search", "postgres")
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "extension search", stdout, []string{
		"PACKAGE", "Harnest_Extension_Postgres", "harnest-extension-postgres-tools",
		"1.2.3", "community",
		"https://pypi.org/project/Harnest_Extension_Postgres/",
	})
	if strings.Contains(stdout, "ordinary-package") || strings.Contains(stdout, "slack") {
		t.Fatalf("search leaked unmatched packages:\n%s", stdout)
	}

	if _, _, err := executeForTest(t, sys, "extensions", "search", "postgres"); err != nil {
		t.Fatal(err)
	}
	if catalogRequests != 1 || metadataRequests != 6 || wheelRequests != 3 {
		t.Fatalf(
			"requests = catalog %d metadata %d wheels %d, want 1, 6, and 3",
			catalogRequests, metadataRequests, wheelRequests,
		)
	}
	cache := filepath.Join(cacheRoot, "harnest", "extensions", "pypi.json")
	contents := string(mustReadTestFile(t, cache))
	if strings.Contains(contents, "ordinary-package") || !strings.Contains(contents, "harnest-extension-slack") {
		t.Fatalf("cache did not retain only the extension namespace:\n%s", contents)
	}
}

// extensionCatalogFixture serves a mixed PyPI index plus exact project metadata.
func extensionCatalogFixture(
	t *testing.T, catalogRequests, metadataRequests, wheelRequests *int,
) http.RoundTripper {
	t.Helper()
	wheels := map[string][]byte{
		"Harnest_Extension_Postgres": searchExtensionWheelFixture(
			t, "Harnest_Extension_Postgres", "postgres", "harnest_extension_postgres", "1.2.3",
		),
		"harnest-extension-postgres-tools": searchExtensionWheelFixture(
			t, "harnest-extension-postgres-tools", "postgres_tools", "harnest_extension_postgres_tools", "1.2.3",
		),
		// A namespace claim without the required entry point must not be shown.
		"harnest-extension-postgres-bogus": searchExtensionWheelFixture(
			t, "harnest-extension-postgres-bogus", "wrong", "harnest_extension_postgres_bogus", "1.2.3",
		),
	}
	return extensionRoundTripFunc(func(request *http.Request) (*http.Response, error) {
		if strings.HasPrefix(request.URL.Path, "/files/") {
			*wheelRequests++
			name := strings.TrimSuffix(strings.TrimPrefix(request.URL.Path, "/files/"), ".whl")
			return extensionHTTPBytesResponse(http.StatusOK, wheels[name], nil), nil
		}
		if strings.HasPrefix(request.URL.Path, "/pypi/") {
			*metadataRequests++
			name := strings.TrimSuffix(strings.TrimPrefix(request.URL.Path, "/pypi/"), "/json")
			body := extensionMetadataFixture(t, name, wheels[name], "1.2.3")
			return extensionHTTPResponse(http.StatusOK, body, nil), nil
		}
		*catalogRequests++
		if request.Header.Get("Accept") != pypiSimpleJSONMediaType {
			t.Errorf("Accept = %q", request.Header.Get("Accept"))
		}
		body := `{"meta":{"api-version":"1.4"},"projects":[` +
			`{"name":"ordinary-package"},` +
			`{"name":"Harnest_Extension_Postgres"},` +
			`{"name":"harnest-extension-postgres-bogus"},` +
			`{"name":"harnest-extension-postgres-tools"},` +
			`{"name":"harnest-extension-slack"}]}`
		return extensionHTTPResponse(
			http.StatusOK, body, map[string]string{"ETag": `"catalog-one"`},
		), nil
	})
}

func TestExtensionsSearchRefreshesWithETagAndSupportsJSON(t *testing.T) {
	var catalogRequests int
	wheel := searchExtensionWheelFixture(
		t, "harnest-extension-slack", "slack", "harnest_extension_slack", "2.0.0",
	)
	transport := extensionRefreshFixture(t, &catalogRequests, wheel)

	sys := extensionSearchTestSystem(transport, t.TempDir())
	if _, _, err := executeForTest(t, sys, "extensions", "search", "slack"); err != nil {
		t.Fatal(err)
	}
	stdout, _, err := executeForTest(
		t, sys, "extensions", "search", "slack", "--refresh", "--json",
	)
	if err != nil {
		t.Fatal(err)
	}
	var results []extensionSearchResult
	if err := json.Unmarshal([]byte(stdout), &results); err != nil {
		t.Fatalf("decode JSON output %q: %v", stdout, err)
	}
	if len(results) != 1 || results[0].Name != "harnest-extension-slack" ||
		results[0].Version != "2.0.0" || results[0].Trust != "community" {
		t.Fatalf("unexpected JSON results: %#v", results)
	}
}

// extensionRefreshFixture serves conditional catalog and compatible wheel responses.
func extensionRefreshFixture(
	t *testing.T, catalogRequests *int, wheel []byte,
) http.RoundTripper {
	t.Helper()
	return extensionRoundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.URL.Path == "/simple/" {
			*catalogRequests++
			if *catalogRequests == 2 {
				if request.Header.Get("If-None-Match") != `"catalog-one"` {
					t.Errorf("If-None-Match = %q", request.Header.Get("If-None-Match"))
				}
				return extensionHTTPResponse(http.StatusNotModified, "", nil), nil
			}
			return extensionHTTPResponse(
				http.StatusOK,
				`{"projects":[{"name":"harnest-extension-slack"}]}`,
				map[string]string{"ETag": `"catalog-one"`},
			), nil
		}
		if request.URL.Path == "/files/harnest-extension-slack.whl" {
			return extensionHTTPBytesResponse(http.StatusOK, wheel, nil), nil
		}
		return extensionHTTPResponse(http.StatusOK,
			extensionMetadataFixture(t, "harnest-extension-slack", wheel, "2.0.0"), nil), nil
	})
}

func TestExtensionsSearchUsesStaleCacheWhenPyPIIsUnavailable(t *testing.T) {
	transport := extensionRoundTripFunc(func(_ *http.Request) (*http.Response, error) {
		return extensionHTTPResponse(http.StatusServiceUnavailable, "", nil), nil
	})
	cacheRoot := t.TempDir()
	cache := extensionCatalogCache{
		Version:   extensionCatalogCacheVersion,
		FetchedAt: time.Now().Add(-time.Hour),
		ETag:      `"stale"`,
		Projects:  []string{"harnest-extension-offline"},
	}
	path := filepath.Join(cacheRoot, "harnest", "extensions", "pypi.json")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := writeExtensionCatalogCache(path, cache); err != nil {
		t.Fatal(err)
	}

	stdout, stderr, err := executeForTest(
		t, extensionSearchTestSystem(transport, cacheRoot),
		"extensions", "search", "offline",
	)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout, "No compatible") || !strings.Contains(stderr, "using cached") {
		t.Fatalf("stdout=%q stderr=%q", stdout, stderr)
	}
}

func TestExtensionWheelInspectionBindsEveryIdentity(t *testing.T) {
	wheel := searchExtensionWheelFixture(
		t, "harnest-extension-postgres", "postgres", "harnest_extension_postgres", "1.2.3",
	)
	if err := inspectExtensionWheel(wheel, "harnest-extension-postgres", "1.2.3"); err != nil {
		t.Fatal(err)
	}
	for label, identity := range map[string][2]string{
		"project": {"harnest-extension-other", "1.2.3"},
		"release": {"harnest-extension-postgres", "2.0.0"},
	} {
		if err := inspectExtensionWheel(wheel, identity[0], identity[1]); err == nil {
			t.Fatalf("%s identity mismatch was accepted", label)
		}
	}
}

func TestExtensionSearchValidationAndRanking(t *testing.T) {
	projects := []string{
		"harnest-extension-postgres-tools",
		"harnest-extension-my-postgres",
		"harnest-extension-postgres",
	}
	got := matchingExtensionProjects(projects, "harnest extension postgres", 2)
	want := []string{"harnest-extension-postgres", "harnest-extension-postgres-tools"}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("ranking = %v, want %v", got, want)
	}
	if validPyPIProjectName("harnest-extension-bad\nname") {
		t.Fatal("unsafe project name was accepted")
	}
	if trust := classifyExtensionProject(
		"Harnest_Extension_Official", []string{"harnest-extension-official"},
	); trust != "official" {
		t.Fatalf("explicit Fused policy returned %q", trust)
	}
	_, _, err := executeForTest(t, defaultSystem(), "extensions", "search", "x", "--limit", "0")
	if err == nil || !strings.Contains(err.Error(), "between 1 and 50") {
		t.Fatalf("limit validation error = %v", err)
	}
}

// extensionSearchTestSystem redirects public network and cache ownership into a fixture.
func extensionSearchTestSystem(transport http.RoundTripper, cacheRoot string) system {
	sys := defaultSystem()
	sys.httpClient = &http.Client{Transport: transport}
	sys.pypiBaseURL = "https://pypi.test"
	sys.userCacheDir = func() (string, error) { return cacheRoot, nil }
	return sys
}

// extensionHTTPResponse builds the minimal response contract consumed by the client.
func extensionHTTPResponse(
	status int, body string, headers map[string]string,
) *http.Response {
	return extensionHTTPBytesResponse(status, []byte(body), headers)
}

// extensionHTTPBytesResponse preserves wheel bytes in HTTP transport fixtures.
func extensionHTTPBytesResponse(
	status int, body []byte, headers map[string]string,
) *http.Response {
	values := make(http.Header)
	for name, value := range headers {
		values.Set(name, value)
	}
	return &http.Response{
		StatusCode:    status,
		Header:        values,
		Body:          io.NopCloser(bytes.NewReader(body)),
		ContentLength: int64(len(body)),
	}
}

// searchExtensionWheelFixture authors the minimal static distribution contract.
func searchExtensionWheelFixture(
	t *testing.T, project, entryName, module, release string,
) []byte {
	t.Helper()
	var buffer bytes.Buffer
	writer := zip.NewWriter(&buffer)
	distInfo := strings.ReplaceAll(project, "-", "_") + "-" + release + ".dist-info"
	files := map[string]string{
		distInfo + "/entry_points.txt": fmt.Sprintf(
			"[%s]\n%s = %s.extension:extension\n", extensionEntryPointGroup, entryName, module,
		),
		strings.ReplaceAll(module, ".", "/") + "/extension.yaml": fmt.Sprintf(
			"apiVersion: harnest.dev/v1alpha1\nkind: Extension\nmetadata:\n  name: %s\n  version: %s\nruntime:\n  entrypoint: extension:extension\n",
			entryName, release,
		),
		strings.ReplaceAll(module, ".", "/") + "/extension.py": "extension = object()\n",
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

func extensionReleaseFile(project string, wheel []byte) pypiReleaseFile {
	digest := sha256.Sum256(wheel)
	filename := project + "-1.0.0-py3-none-any.whl"
	artifact := pypiReleaseFile{
		Filename: filename, PackageType: "bdist_wheel",
		URL:  "https://pypi.test/files/" + project + ".whl",
		Size: int64(len(wheel)),
	}
	artifact.Digests.SHA256 = hex.EncodeToString(digest[:])
	return artifact
}

func TestSelectExtensionWheelRequiresUniversalCompatibilityTags(t *testing.T) {
	files := []pypiReleaseFile{
		extensionReleaseFile("demo-1.0.0-cp313-cp313-win_amd64", []byte("windows")),
		extensionReleaseFile("demo-1.0.0-py3-none-any", []byte("universal")),
	}
	files[0].Filename = "demo-1.0.0-cp313-cp313-win_amd64.whl"
	files[1].Filename = "demo-1.0.0-py3-none-any.whl"

	selected, err := selectExtensionWheel(files)
	if err != nil {
		t.Fatal(err)
	}
	if selected.Filename != files[1].Filename {
		t.Fatalf("selected wheel = %q, want universal wheel", selected.Filename)
	}

	if _, err := selectExtensionWheel(files[:1]); err == nil ||
		!strings.Contains(err.Error(), "py3-none-any") {
		t.Fatalf("platform-only wheel error = %v", err)
	}
}

func extensionMetadataFixture(
	t *testing.T, project string, wheel []byte, release string,
) string {
	t.Helper()
	metadata := pypiProjectMetadata{URLs: []pypiReleaseFile{extensionReleaseFile(project, wheel)}}
	metadata.Info.Name = project
	metadata.Info.Version = release
	metadata.Info.Summary = "Extension for " + project
	contents, err := json.Marshal(metadata)
	if err != nil {
		t.Fatal(err)
	}
	return string(contents)
}
