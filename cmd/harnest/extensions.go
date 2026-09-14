package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"text/tabwriter"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/spf13/cobra"
)

const (
	pypiExtensionPrefix          = "harnest-extension-"
	pypiSimpleJSONMediaType      = "application/vnd.pypi.simple.v1+json"
	extensionCatalogCacheVersion = 2
	extensionCatalogTTL          = 10 * time.Minute
	maxExtensionCatalogBytes     = 64 * 1024 * 1024
	maxExtensionMetadataBytes    = 2 * 1024 * 1024
	maxExtensionCacheBytes       = 4 * 1024 * 1024
)

type extensionCatalogCache struct {
	Version   int       `json:"version"`
	FetchedAt time.Time `json:"fetchedAt"`
	ETag      string    `json:"etag,omitempty"`
	Projects  []string  `json:"projects"`
}

type extensionSearchResult struct {
	Name        string `json:"name"`
	Version     string `json:"version,omitempty"`
	Description string `json:"description,omitempty"`
	Trust       string `json:"trust"`
	URL         string `json:"url"`
}

type pypiProjectMetadata struct {
	Info struct {
		Name    string `json:"name"`
		Version string `json:"version"`
		Summary string `json:"summary"`
	} `json:"info"`
	URLs []pypiReleaseFile `json:"urls"`
}

// newExtensionsCommand owns local authoring and public package discovery.
func (a *application) newExtensionsCommand() *cobra.Command {
	command := &cobra.Command{
		Use:   "extensions",
		Short: "Create, install, and discover Harnest Extensions",
		Long: `Discover Harnest Extensions on public PyPI, or create and install an
application-local package, without importing package code during the CLI operation.

Publish harnest-extension-* packages with one harnest.extensions entry point,
such as "postgres = harnest_extension_postgres.extension:extension", and include
extension.yaml plus extension.py. The manifest's contributes mapping explicitly
projects package-relative lifecycle, MCP, skill, subagent, and tool directories.
Search does not install packages or convert
their layouts. Use harnest upgrade to migrate retired RuntimePlugin projects.

The harnest-extension-* name is only a candidate namespace. Search results must
also contain one harnest.extensions entry point and its extension.yaml/extension.py
bundle in a digest-verified wheel; package code is never imported by search.
For harnest-extension-postgres, publish an entry point such as
"postgres = harnest_extension_postgres.extension:extension" in the harnest.extensions
group and package harnest_extension_postgres/extension.yaml plus
harnest_extension_postgres/extension.py.

Trust is reported separately: community packages satisfy the bundle contract,
while official packages are names explicitly owned and approved by Fused.
Neither label is a security review of the extension's code.`,
	}
	command.AddCommand(
		a.newExtensionInitCommand(),
		a.newExtensionInstallCommand(),
		a.newExtensionSearchCommand(),
	)
	return command
}

// newExtensionSearchCommand searches only the public Harnest package namespace.
func (a *application) newExtensionSearchCommand() *cobra.Command {
	var limit int
	var refresh bool
	var jsonOutput bool
	command := &cobra.Command{
		Use:   "search [QUERY]",
		Short: "Search Harnest Extension packages on PyPI",
		Args:  cobra.MaximumNArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			if limit < 1 || limit > 50 {
				return fmt.Errorf("--limit must be between 1 and 50")
			}
			query := ""
			if len(arguments) == 1 {
				query = arguments[0]
			}
			results, stale, err := a.searchPyPIExtensions(
				command.Context(), query, limit, refresh,
			)
			if err != nil {
				return err
			}
			if stale {
				fmt.Fprintln(
					command.ErrOrStderr(),
					"harnest: PyPI unavailable; using cached extension catalog",
				)
			}
			return renderExtensionSearch(command.OutOrStdout(), results, jsonOutput)
		},
	}
	command.Flags().IntVar(&limit, "limit", 20, "maximum results (1-50)")
	command.Flags().BoolVar(&refresh, "refresh", false, "refresh the cached PyPI catalog")
	command.Flags().BoolVar(&jsonOutput, "json", false, "print machine-readable JSON")
	return command
}

// searchPyPIExtensions returns only candidates whose immutable wheel satisfies Harnest.
func (a *application) searchPyPIExtensions(
	ctx context.Context, query string, limit int, refresh bool,
) ([]extensionSearchResult, bool, error) {
	catalog, stale, err := a.loadPyPIExtensionCatalog(ctx, refresh)
	if err != nil {
		return nil, false, err
	}
	// A bounded surplus lets invalid namespace claims get filtered without making
	// one search fan out across an attacker-controlled number of distributions.
	candidateLimit := limit * 3
	if candidateLimit > 50 {
		candidateLimit = 50
	}
	names := matchingExtensionProjects(catalog, query, candidateLimit)
	results := make([]extensionSearchResult, 0, len(names))
	for _, name := range names {
		metadata, metadataErr := a.fetchPyPIExtensionMetadata(ctx, name, stale)
		if metadataErr != nil || stale {
			continue
		}
		inspection, inspectionErr := a.inspectPyPIExtension(ctx, name, metadata)
		if inspectionErr != nil || !inspection.Compatible {
			continue
		}
		result := extensionSearchResult{
			Name: name, Version: cleanExtensionVersion(metadata.Info.Version),
			Description: cleanExtensionDescription(metadata.Info.Summary),
			Trust:       extensionProjectTrust(name),
			URL:         "https://pypi.org/project/" + url.PathEscape(name) + "/",
		}
		results = append(results, result)
		if len(results) == limit {
			break
		}
	}
	return results, stale, nil
}

// loadPyPIExtensionCatalog reuses fresh state and falls back to stale state offline.
func (a *application) loadPyPIExtensionCatalog(
	ctx context.Context, refresh bool,
) ([]string, bool, error) {
	path, err := a.extensionCatalogCachePath()
	if err != nil {
		return nil, false, err
	}
	cached, found := readExtensionCatalogCache(path)
	if found && !refresh && time.Since(cached.FetchedAt) < extensionCatalogTTL {
		return cached.Projects, false, nil
	}
	updated, err := a.fetchPyPIExtensionCatalog(ctx, cached, found)
	if err != nil && found {
		return cached.Projects, true, nil
	}
	if err != nil {
		return nil, false, err
	}
	if err := writeExtensionCatalogCache(path, updated); err != nil {
		return nil, false, err
	}
	return updated.Projects, false, nil
}

// fetchPyPIExtensionCatalog streams PyPI's full index while retaining only extensions.
func (a *application) fetchPyPIExtensionCatalog(
	ctx context.Context, cached extensionCatalogCache, hasCache bool,
) (extensionCatalogCache, error) {
	request, err := a.newPyPIRequest(ctx, http.MethodGet, "/simple/")
	if err != nil {
		return extensionCatalogCache{}, err
	}
	request.Header.Set("Accept", pypiSimpleJSONMediaType)
	if hasCache && cached.ETag != "" {
		request.Header.Set("If-None-Match", cached.ETag)
	}
	response, err := a.extensionHTTPClient().Do(request)
	if err != nil {
		return extensionCatalogCache{}, fmt.Errorf("query PyPI extension catalog: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusNotModified && hasCache {
		cached.FetchedAt = time.Now().UTC()
		return cached, nil
	}
	if response.StatusCode != http.StatusOK {
		return extensionCatalogCache{}, fmt.Errorf(
			"query PyPI extension catalog: HTTP %d", response.StatusCode,
		)
	}
	if response.ContentLength > maxExtensionCatalogBytes {
		return extensionCatalogCache{}, fmt.Errorf(
			"PyPI extension catalog exceeds %d bytes", maxExtensionCatalogBytes,
		)
	}
	projects, err := decodePyPIExtensionProjects(
		io.LimitReader(response.Body, maxExtensionCatalogBytes+1),
	)
	if err != nil {
		return extensionCatalogCache{}, fmt.Errorf("decode PyPI extension catalog: %w", err)
	}
	return extensionCatalogCache{
		Version: extensionCatalogCacheVersion, FetchedAt: time.Now().UTC(),
		ETag: response.Header.Get("ETag"), Projects: projects,
	}, nil
}

// decodePyPIExtensionProjects avoids retaining PyPI's complete project index.
func decodePyPIExtensionProjects(reader io.Reader) ([]string, error) {
	decoder := json.NewDecoder(reader)
	token, err := decoder.Token()
	if err != nil || token != json.Delim('{') {
		return nil, fmt.Errorf("expected a JSON object")
	}
	projects := []string{}
	for decoder.More() {
		key, keyErr := decoder.Token()
		if keyErr != nil {
			return nil, keyErr
		}
		if key != "projects" {
			var ignored json.RawMessage
			if err := decoder.Decode(&ignored); err != nil {
				return nil, err
			}
			continue
		}
		values, valuesErr := decodePyPIProjectArray(decoder)
		if valuesErr != nil {
			return nil, valuesErr
		}
		projects = append(projects, values...)
	}
	if _, err := decoder.Token(); err != nil {
		return nil, err
	}
	if _, err := decoder.Token(); err != io.EOF {
		return nil, fmt.Errorf("unexpected data after PyPI project index")
	}
	sort.Slice(projects, func(i, j int) bool {
		return normalizeProjectName(projects[i]) < normalizeProjectName(projects[j])
	})
	return deduplicateExtensionProjects(projects), nil
}

// decodePyPIProjectArray filters the supported Simple API project array in-stream.
func decodePyPIProjectArray(decoder *json.Decoder) ([]string, error) {
	token, err := decoder.Token()
	if err != nil || token != json.Delim('[') {
		return nil, fmt.Errorf("PyPI projects must be an array")
	}
	projects := []string{}
	for decoder.More() {
		var project struct {
			Name string `json:"name"`
		}
		if err := decoder.Decode(&project); err != nil {
			return nil, err
		}
		if validPyPIProjectName(project.Name) && isExtensionProject(project.Name) {
			projects = append(projects, project.Name)
		}
	}
	_, err = decoder.Token()
	return projects, err
}

// deduplicateExtensionProjects removes equivalent PEP 503 names deterministically.
func deduplicateExtensionProjects(projects []string) []string {
	result := make([]string, 0, len(projects))
	previous := ""
	for _, project := range projects {
		normalized := normalizeProjectName(project)
		if normalized == previous {
			continue
		}
		result = append(result, project)
		previous = normalized
	}
	return result
}

// matchingExtensionProjects applies stable exact, prefix, then substring ranking.
func matchingExtensionProjects(projects []string, query string, limit int) []string {
	normalizedQuery := normalizeExtensionQuery(query)
	type rankedProject struct {
		name string
		rank int
	}
	ranked := []rankedProject{}
	for _, project := range projects {
		slug := extensionProjectSlug(project)
		rank, matches := extensionProjectRank(slug, normalizedQuery)
		if matches {
			ranked = append(ranked, rankedProject{name: project, rank: rank})
		}
	}
	sort.Slice(ranked, func(i, j int) bool {
		if ranked[i].rank != ranked[j].rank {
			return ranked[i].rank < ranked[j].rank
		}
		return normalizeProjectName(ranked[i].name) < normalizeProjectName(ranked[j].name)
	})
	if len(ranked) > limit {
		ranked = ranked[:limit]
	}
	result := make([]string, len(ranked))
	for index, project := range ranked {
		result[index] = project.name
	}
	return result
}

func extensionProjectRank(slug, query string) (int, bool) {
	if query == "" || slug == query {
		return 0, true
	}
	if strings.HasPrefix(slug, query) {
		return 1, true
	}
	if strings.Contains(slug, query) {
		return 2, true
	}
	return 0, false
}

// normalizeProjectName implements the PEP 503 comparison form.
func normalizeProjectName(value string) string {
	var builder strings.Builder
	separator := false
	for _, character := range strings.ToLower(strings.TrimSpace(value)) {
		if character == '-' || character == '_' || character == '.' {
			separator = true
			continue
		}
		if separator && builder.Len() > 0 {
			builder.WriteByte('-')
		}
		separator = false
		builder.WriteRune(character)
	}
	return builder.String()
}

func normalizeExtensionQuery(value string) string {
	joined := strings.Join(strings.Fields(value), "-")
	return extensionProjectSlug(joined)
}

// validPyPIProjectName rejects cache or response text that is unsafe to render.
func validPyPIProjectName(value string) bool {
	if len(value) == 0 || len(value) > 200 || !asciiAlphaNumeric(value[0]) || !asciiAlphaNumeric(value[len(value)-1]) {
		return false
	}
	for index := 1; index < len(value)-1; index++ {
		character := value[index]
		if !asciiAlphaNumeric(character) && character != '-' && character != '_' && character != '.' {
			return false
		}
	}
	return true
}

func asciiAlphaNumeric(character byte) bool {
	return character >= 'a' && character <= 'z' ||
		character >= 'A' && character <= 'Z' ||
		character >= '0' && character <= '9'
}

// fetchPyPIExtensionMetadata enriches a matched package without controlling discovery.
func (a *application) fetchPyPIExtensionMetadata(
	ctx context.Context, name string, offline bool,
) (pypiProjectMetadata, error) {
	if offline {
		return pypiProjectMetadata{}, fmt.Errorf("PyPI metadata unavailable offline")
	}
	request, err := a.newPyPIRequest(
		ctx, http.MethodGet, "/pypi/"+url.PathEscape(name)+"/json",
	)
	if err != nil {
		return pypiProjectMetadata{}, err
	}
	response, err := a.extensionHTTPClient().Do(request)
	if err != nil {
		return pypiProjectMetadata{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return pypiProjectMetadata{}, fmt.Errorf(
			"PyPI metadata returned HTTP %d", response.StatusCode,
		)
	}
	var metadata pypiProjectMetadata
	decoder := json.NewDecoder(io.LimitReader(response.Body, maxExtensionMetadataBytes+1))
	if err := decoder.Decode(&metadata); err != nil {
		return pypiProjectMetadata{}, err
	}
	if response.ContentLength > maxExtensionMetadataBytes {
		return pypiProjectMetadata{}, fmt.Errorf("PyPI metadata exceeds its limit")
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return pypiProjectMetadata{}, fmt.Errorf("unexpected data after PyPI metadata")
	}
	return metadata, nil
}

// newPyPIRequest keeps the public host and CLI identity consistent.
func (a *application) newPyPIRequest(
	ctx context.Context, method, path string,
) (*http.Request, error) {
	base := strings.TrimRight(a.system.pypiBaseURL, "/")
	if base == "" {
		base = "https://pypi.org"
	}
	request, err := http.NewRequestWithContext(ctx, method, base+path, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("User-Agent", "harnest/"+a.version)
	return request, nil
}

func (a *application) extensionHTTPClient() *http.Client {
	if a.system.httpClient != nil {
		return a.system.httpClient
	}
	return &http.Client{Timeout: 30 * time.Second}
}

func (a *application) extensionCatalogCachePath() (string, error) {
	cacheDirectory := a.system.userCacheDir
	if cacheDirectory == nil {
		cacheDirectory = os.UserCacheDir
	}
	root, err := cacheDirectory()
	if err != nil {
		return "", fmt.Errorf("resolve user cache directory: %w", err)
	}
	return filepath.Join(root, "harnest", "extensions", "pypi.json"), nil
}

// readExtensionCatalogCache treats untrusted or obsolete cache state as a miss.
func readExtensionCatalogCache(path string) (extensionCatalogCache, bool) {
	info, err := os.Lstat(path)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() || info.Size() > maxExtensionCacheBytes {
		return extensionCatalogCache{}, false
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return extensionCatalogCache{}, false
	}
	var cached extensionCatalogCache
	if json.Unmarshal(contents, &cached) != nil || !validExtensionCatalogCache(cached) {
		return extensionCatalogCache{}, false
	}
	return cached, true
}

func validExtensionCatalogCache(cached extensionCatalogCache) bool {
	if cached.Version != extensionCatalogCacheVersion || cached.FetchedAt.IsZero() || cached.FetchedAt.After(time.Now().Add(5*time.Minute)) {
		return false
	}
	previous := ""
	for _, project := range cached.Projects {
		normalized := normalizeProjectName(project)
		if !validPyPIProjectName(project) || !isExtensionProject(normalized) || normalized <= previous {
			return false
		}
		previous = normalized
	}
	return true
}

// writeExtensionCatalogCache atomically publishes only the filtered public index.
func writeExtensionCatalogCache(path string, cached extensionCatalogCache) error {
	contents, err := json.Marshal(cached)
	if err != nil {
		return err
	}
	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0o755); err != nil {
		return fmt.Errorf("create extension cache directory: %w", err)
	}
	temporary, err := os.CreateTemp(directory, ".pypi-*.json")
	if err != nil {
		return fmt.Errorf("create extension cache: %w", err)
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(0o600); err != nil {
		temporary.Close()
		return err
	}
	if _, err := temporary.Write(contents); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return fmt.Errorf("publish extension cache: %w", err)
	}
	return nil
}

// cleanExtensionDescription renders remote text without terminal spoofing controls.
func cleanExtensionDescription(value string) string {
	// Format controls can visually reorder terminal output even though they are
	// not traditional ASCII control bytes.
	printable := strings.Map(func(character rune) rune {
		if unicode.IsControl(character) || unicode.In(character, unicode.Cf) {
			return -1
		}
		return character
	}, value)
	cleaned := strings.Join(strings.Fields(printable), " ")
	if utf8.RuneCountInString(cleaned) <= 100 {
		return cleaned
	}
	return string([]rune(cleaned)[:97]) + "..."
}

func cleanExtensionVersion(value string) string {
	cleaned := strings.Join(strings.Fields(value), "")
	if utf8.RuneCountInString(cleaned) <= 50 {
		return cleaned
	}
	return string([]rune(cleaned)[:50])
}

// renderExtensionSearch keeps human output compact and offers stable JSON for agents.
func renderExtensionSearch(
	output io.Writer, results []extensionSearchResult, jsonOutput bool,
) error {
	if jsonOutput {
		encoder := json.NewEncoder(output)
		encoder.SetEscapeHTML(false)
		return encoder.Encode(results)
	}
	if len(results) == 0 {
		_, err := fmt.Fprintln(output, "No compatible Harnest Extensions found on PyPI.")
		return err
	}
	writer := tabwriter.NewWriter(output, 0, 4, 2, ' ', 0)
	if _, err := fmt.Fprintln(writer, "PACKAGE\tVERSION\tTRUST\tDESCRIPTION"); err != nil {
		return err
	}
	for _, result := range results {
		version := result.Version
		if version == "" {
			version = "-"
		}
		if _, err := fmt.Fprintf(
			writer, "%s\t%s\t%s\t%s\n",
			result.Name, version, result.Trust, result.Description,
		); err != nil {
			return err
		}
		if _, err := fmt.Fprintf(writer, "\t\t\t%s\n", result.URL); err != nil {
			return err
		}
	}
	return writer.Flush()
}
