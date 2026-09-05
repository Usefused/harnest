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

type pluginRoundTripFunc func(*http.Request) (*http.Response, error)

func (function pluginRoundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func TestPluginsSearchFiltersPyPIAndReusesFreshCatalog(t *testing.T) {
	var catalogRequests int
	var metadataRequests int
	var wheelRequests int
	transport := pluginCatalogFixture(
		t, &catalogRequests, &metadataRequests, &wheelRequests,
	)

	cacheRoot := t.TempDir()
	sys := pluginSearchTestSystem(transport, cacheRoot)
	stdout, _, err := executeForTest(t, sys, "extensions", "search", "postgres")
	if err != nil {
		t.Fatal(err)
	}
	assertContainsAll(t, "plugin search", stdout, []string{
		"PACKAGE", "Harnest_Plugin_Postgres", "harnest-plugin-postgres-tools",
		"1.2.3", "community",
		"https://pypi.org/project/Harnest_Plugin_Postgres/",
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
	cache := filepath.Join(cacheRoot, "harnest", "plugins", "pypi.json")
	contents := string(mustReadTestFile(t, cache))
	if strings.Contains(contents, "ordinary-package") || !strings.Contains(contents, "harnest-plugin-slack") {
		t.Fatalf("cache did not retain only the plugin namespace:\n%s", contents)
	}
}

// pluginCatalogFixture serves a mixed PyPI index plus exact project metadata.
func pluginCatalogFixture(
	t *testing.T, catalogRequests, metadataRequests, wheelRequests *int,
) http.RoundTripper {
	t.Helper()
	wheels := map[string][]byte{
		"Harnest_Plugin_Postgres": pluginWheelFixture(
			t, "Harnest_Plugin_Postgres", "postgres", "harnest_plugin_postgres", "1.2.3",
		),
		"harnest-plugin-postgres-tools": pluginWheelFixture(
			t, "harnest-plugin-postgres-tools", "postgres_tools", "harnest_plugin_postgres_tools", "1.2.3",
		),
		// A namespace claim without the required entry point must not be shown.
		"harnest-plugin-postgres-bogus": pluginWheelFixture(
			t, "harnest-plugin-postgres-bogus", "wrong", "harnest_plugin_postgres_bogus", "1.2.3",
		),
	}
	return pluginRoundTripFunc(func(request *http.Request) (*http.Response, error) {
		if strings.HasPrefix(request.URL.Path, "/files/") {
			*wheelRequests++
			name := strings.TrimSuffix(strings.TrimPrefix(request.URL.Path, "/files/"), ".whl")
			return pluginHTTPBytesResponse(http.StatusOK, wheels[name], nil), nil
		}
		if strings.HasPrefix(request.URL.Path, "/pypi/") {
			*metadataRequests++
			name := strings.TrimSuffix(strings.TrimPrefix(request.URL.Path, "/pypi/"), "/json")
			body := pluginMetadataFixture(t, name, wheels[name], "1.2.3")
			return pluginHTTPResponse(http.StatusOK, body, nil), nil
		}
		*catalogRequests++
		if request.Header.Get("Accept") != pypiSimpleJSONMediaType {
			t.Errorf("Accept = %q", request.Header.Get("Accept"))
		}
		body := `{"meta":{"api-version":"1.4"},"projects":[` +
			`{"name":"ordinary-package"},` +
			`{"name":"Harnest_Plugin_Postgres"},` +
			`{"name":"harnest-plugin-postgres-bogus"},` +
			`{"name":"harnest-plugin-postgres-tools"},` +
			`{"name":"harnest-plugin-slack"}]}`
		return pluginHTTPResponse(
			http.StatusOK, body, map[string]string{"ETag": `"catalog-one"`},
		), nil
	})
}

func TestPluginsSearchRefreshesWithETagAndSupportsJSON(t *testing.T) {
	var catalogRequests int
	wheel := pluginWheelFixture(
		t, "harnest-plugin-slack", "slack", "harnest_plugin_slack", "2.0.0",
	)
	transport := pluginRefreshFixture(t, &catalogRequests, wheel)

	sys := pluginSearchTestSystem(transport, t.TempDir())
	if _, _, err := executeForTest(t, sys, "extensions", "search", "slack"); err != nil {
		t.Fatal(err)
	}
	stdout, _, err := executeForTest(
		t, sys, "extensions", "search", "slack", "--refresh", "--json",
	)
	if err != nil {
		t.Fatal(err)
	}
	var results []pluginSearchResult
	if err := json.Unmarshal([]byte(stdout), &results); err != nil {
		t.Fatalf("decode JSON output %q: %v", stdout, err)
	}
	if len(results) != 1 || results[0].Name != "harnest-plugin-slack" ||
		results[0].Version != "2.0.0" || results[0].Trust != "community" {
		t.Fatalf("unexpected JSON results: %#v", results)
	}
}

// pluginRefreshFixture serves conditional catalog and compatible wheel responses.
func pluginRefreshFixture(
	t *testing.T, catalogRequests *int, wheel []byte,
) http.RoundTripper {
	t.Helper()
	return pluginRoundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.URL.Path == "/simple/" {
			*catalogRequests++
			if *catalogRequests == 2 {
				if request.Header.Get("If-None-Match") != `"catalog-one"` {
					t.Errorf("If-None-Match = %q", request.Header.Get("If-None-Match"))
				}
				return pluginHTTPResponse(http.StatusNotModified, "", nil), nil
			}
			return pluginHTTPResponse(
				http.StatusOK,
				`{"projects":[{"name":"harnest-plugin-slack"}]}`,
				map[string]string{"ETag": `"catalog-one"`},
			), nil
		}
		if request.URL.Path == "/files/harnest-plugin-slack.whl" {
			return pluginHTTPBytesResponse(http.StatusOK, wheel, nil), nil
		}
		return pluginHTTPResponse(http.StatusOK,
			pluginMetadataFixture(t, "harnest-plugin-slack", wheel, "2.0.0"), nil), nil
	})
}

func TestPluginsSearchUsesStaleCacheWhenPyPIIsUnavailable(t *testing.T) {
	transport := pluginRoundTripFunc(func(_ *http.Request) (*http.Response, error) {
		return pluginHTTPResponse(http.StatusServiceUnavailable, "", nil), nil
	})
	cacheRoot := t.TempDir()
	cache := pluginCatalogCache{
		Version:   pluginCatalogCacheVersion,
		FetchedAt: time.Now().Add(-time.Hour),
		ETag:      `"stale"`,
		Projects:  []string{"harnest-plugin-offline"},
	}
	path := filepath.Join(cacheRoot, "harnest", "plugins", "pypi.json")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := writePluginCatalogCache(path, cache); err != nil {
		t.Fatal(err)
	}

	stdout, stderr, err := executeForTest(
		t, pluginSearchTestSystem(transport, cacheRoot),
		"extensions", "search", "offline",
	)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout, "No compatible") || !strings.Contains(stderr, "using cached") {
		t.Fatalf("stdout=%q stderr=%q", stdout, stderr)
	}
}

func TestPluginWheelInspectionBindsEveryIdentity(t *testing.T) {
	wheel := pluginWheelFixture(
		t, "harnest-plugin-postgres", "postgres", "harnest_plugin_postgres", "1.2.3",
	)
	if err := inspectPluginWheel(wheel, "harnest-plugin-postgres", "1.2.3"); err != nil {
		t.Fatal(err)
	}
	for label, identity := range map[string][2]string{
		"project": {"harnest-plugin-other", "1.2.3"},
		"release": {"harnest-plugin-postgres", "2.0.0"},
	} {
		if err := inspectPluginWheel(wheel, identity[0], identity[1]); err == nil {
			t.Fatalf("%s identity mismatch was accepted", label)
		}
	}
}

func TestPluginSearchValidationAndRanking(t *testing.T) {
	projects := []string{
		"harnest-plugin-postgres-tools",
		"harnest-plugin-my-postgres",
		"harnest-plugin-postgres",
	}
	got := matchingPluginProjects(projects, "harnest plugin postgres", 2)
	want := []string{"harnest-plugin-postgres", "harnest-plugin-postgres-tools"}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("ranking = %v, want %v", got, want)
	}
	if validPyPIProjectName("harnest-plugin-bad\nname") {
		t.Fatal("unsafe project name was accepted")
	}
	if trust := classifyPluginProject(
		"Harnest_Plugin_Official", []string{"harnest-plugin-official"},
	); trust != "official" {
		t.Fatalf("explicit Fused policy returned %q", trust)
	}
	_, _, err := executeForTest(t, defaultSystem(), "extensions", "search", "x", "--limit", "0")
	if err == nil || !strings.Contains(err.Error(), "between 1 and 50") {
		t.Fatalf("limit validation error = %v", err)
	}
}

// pluginSearchTestSystem redirects public network and cache ownership into a fixture.
func pluginSearchTestSystem(transport http.RoundTripper, cacheRoot string) system {
	sys := defaultSystem()
	sys.httpClient = &http.Client{Transport: transport}
	sys.pypiBaseURL = "https://pypi.test"
	sys.userCacheDir = func() (string, error) { return cacheRoot, nil }
	return sys
}

// pluginHTTPResponse builds the minimal response contract consumed by the client.
func pluginHTTPResponse(
	status int, body string, headers map[string]string,
) *http.Response {
	return pluginHTTPBytesResponse(status, []byte(body), headers)
}

// pluginHTTPBytesResponse preserves wheel bytes in HTTP transport fixtures.
func pluginHTTPBytesResponse(
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

// pluginWheelFixture authors the minimal static distribution contract.
func pluginWheelFixture(
	t *testing.T, project, entryName, module, release string,
) []byte {
	t.Helper()
	var buffer bytes.Buffer
	writer := zip.NewWriter(&buffer)
	distInfo := strings.ReplaceAll(project, "-", "_") + "-" + release + ".dist-info"
	files := map[string]string{
		distInfo + "/entry_points.txt": fmt.Sprintf(
			"[%s]\n%s = %s.plugin:plugin\n", pluginEntryPointGroup, entryName, module,
		),
		strings.ReplaceAll(module, ".", "/") + "/plugin.yaml": fmt.Sprintf(
			"apiVersion: harnest.dev/v1alpha1\nkind: RuntimePlugin\nmetadata:\n  name: %s\n  version: %s\nruntime:\n  entrypoint: plugin:plugin\n",
			entryName, release,
		),
		strings.ReplaceAll(module, ".", "/") + "/plugin.py": "plugin = object()\n",
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

func pluginReleaseFile(project string, wheel []byte) pypiReleaseFile {
	digest := sha256.Sum256(wheel)
	artifact := pypiReleaseFile{
		Filename: project + ".whl", PackageType: "bdist_wheel",
		URL:  "https://pypi.test/files/" + project + ".whl",
		Size: int64(len(wheel)),
	}
	artifact.Digests.SHA256 = hex.EncodeToString(digest[:])
	return artifact
}

func pluginMetadataFixture(
	t *testing.T, project string, wheel []byte, release string,
) string {
	t.Helper()
	metadata := pypiProjectMetadata{URLs: []pypiReleaseFile{pluginReleaseFile(project, wheel)}}
	metadata.Info.Name = project
	metadata.Info.Version = release
	metadata.Info.Summary = "Plugin for " + project
	contents, err := json.Marshal(metadata)
	if err != nil {
		t.Fatal(err)
	}
	return string(contents)
}
