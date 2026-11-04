package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"

	"gopkg.in/yaml.v3"
	"harnest.dev/harnest/engine"
)

type hookRequest struct {
	Config           map[string]any `json:"config,omitempty"`
	ProvidedBindings []string       `json:"providedBindings,omitempty"`
	Version          int            `json:"version"`
	ProjectDir       string         `json:"projectDir"`
	Architecture     string         `json:"architecture"`
	OutputDir        string         `json:"outputDir,omitempty"`
	Project          map[string]any `json:"project,omitempty"`
	Declarations     *review        `json:"declarations,omitempty"`
}

type review struct {
	SourceDigest   string   `json:"sourceDigest"`
	ArtifactDigest string   `json:"artifactDigest,omitempty"`
	BaseImage      string   `json:"baseImage"`
	Required       []string `json:"requiredBindings,omitempty"`
}

type hookResponse struct {
	Version      int               `json:"version"`
	Project      map[string]any    `json:"project,omitempty"`
	Declarations *review           `json:"declarations,omitempty"`
	Artifact     map[string]string `json:"artifact,omitempty"`
	Error        string            `json:"error,omitempty"`
}

// dispatchHook uses Hostlier's bounded v1 JSON protocol. Public diagnostics never echo the project's values.
func dispatchHook(ctx context.Context, action string, input io.Reader, output io.Writer) error {
	var req hookRequest
	if err := readHookRequest(input, &req); err != nil {
		return err
	}
	result, err := projectHook(ctx, action, req)
	result.Version = 1
	if err != nil {
		result = hookResponse{Version: 1, Error: err.Error()}
	}
	return json.NewEncoder(output).Encode(result)
}

// readHookRequest validates the common version and project directory before local hooks touch the project.
func readHookRequest(input io.Reader, req *hookRequest) error {
	data, err := io.ReadAll(io.LimitReader(input, (4<<20)+1))
	if err != nil || len(data) > 4<<20 || json.Unmarshal(data, req) != nil || req.Version != 1 || !filepath.IsAbs(req.ProjectDir) {
		return errors.New("invalid project hook request")
	}
	return nil
}

// projectHook rebuilds the public plan before packaging to catch modified source, requirements, and precompiled bytes.
func projectHook(ctx context.Context, action string, req hookRequest) (hookResponse, error) {
	var out hookResponse
	bundle, err := engine.LoadBundle(req.ProjectDir)
	if err != nil {
		return out, errors.New("invalid Harnest agent project; validate the source with harnest")
	}
	project, err := readTopology(req.ProjectDir, bundle.Config.Metadata.Name)
	if err != nil {
		return out, err
	}
	settings, err := inlineSettings(req.Config)
	if err != nil {
		return out, err
	}
	port, err := deploymentProject(req, project, bundle, settings)
	if err != nil {
		return out, err
	}
	decl, err := reviewInputs(ctx, req, bundle, settings)
	if err != nil {
		return out, err
	}
	if action == "project-plan" {
		return hookResponse{Project: project, Declarations: &decl}, nil
	}
	if !reflect.DeepEqual(req.Declarations, &decl) || !sameWorkload(req.Project, project) {
		return out, errors.New("deployment inputs changed; run Hostlier plan again")
	}
	return buildReviewed(ctx, req, settings, decl, port)
}

// reviewInputs records the exact base manifest and optional prebuilt agent reviewed for this target.
func reviewInputs(ctx context.Context, req hookRequest, bundle engine.Bundle, settings projectSettings) (review, error) {
	base, err := settings.resolveBaseImage(ctx, req.Architecture)
	if err != nil {
		return review{}, err
	}
	decl := review{SourceDigest: bundle.Digest, BaseImage: base, Required: settings.RequiredBindings}
	if settings.Artifact != "" {
		decl.ArtifactDigest, err = reviewArtifact(req, settings.Artifact, &bundle.Config)
	}
	return decl, err
}

// reviewArtifact checks containment before inspecting prebuilt code and rejects foreign targets during preview.
func reviewArtifact(req hookRequest, name string, config *engine.AgentConfig) (string, error) {
	sum, err := artifactDigest(req.ProjectDir, name)
	if err != nil {
		return "", err
	}
	return sum, verifyAgent(filepath.Join(req.ProjectDir, name), req.Architecture, config)
}

// artifactDigest reviews the actual compiled bytes, including binaries stored in the ignored build directory.
func artifactDigest(root, name string) (string, error) {
	file, err := openProjectFile(root, name)
	if err != nil {
		return "", err
	}
	defer file.Close()
	sum := sha256.New()
	if _, err := io.Copy(sum, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(sum.Sum(nil)), nil
}

// buildReviewed keeps the context inside Hostlier's scratch directory and verifies the staged bytes before returning it.
func buildReviewed(ctx context.Context, req hookRequest, settings projectSettings, decl review, port int) (hookResponse, error) {
	var out hookResponse
	if !filepath.IsAbs(req.OutputDir) {
		return out, errors.New("expected an absolute Hostlier output directory")
	}
	build := buildSettings{project: req.ProjectDir, architecture: req.Architecture, base: decl.BaseImage, port: port, output: filepath.Join(req.OutputDir, "context")}
	if settings.Artifact != "" {
		build.artifact = filepath.Join(req.ProjectDir, settings.Artifact)
	}
	if err := prepare(ctx, build); err != nil {
		return out, err
	}
	if build.artifact != "" {
		sum, err := artifactDigest(build.output, "agent")
		if err != nil || sum != decl.ArtifactDigest {
			return out, errors.New("compiled artifact changed during packaging")
		}
	}
	// A compiler or editor can update source after the hook admission check.
	if digest, err := engine.BundleDigest(req.ProjectDir); err != nil || digest != decl.SourceDigest {
		return out, errors.New("agent source changed during packaging; run Hostlier plan again")
	}
	return hookResponse{Artifact: map[string]string{"kind": "container-context", "path": "context"}}, nil
}

// deploymentProject reads deployment topology as data and keeps the Harnest build confined to the primary workload.
func deploymentProject(req hookRequest, p map[string]any, b engine.Bundle, settings projectSettings) (int, error) {
	// Keep an explicitly configured starter identity bound to the same agent at packaging time.
	if settings.Name != "" && settings.Name != b.Config.Metadata.Name {
		return 0, errors.New("packages.harnest.name must match the agent in config.yaml")
	}
	if req.Architecture != "amd64" && req.Architecture != "arm64" {
		return 0, errors.New("unsupported Hostlier architecture")
	}
	if err := normalizeTopology(p); err != nil {
		return 0, err
	}
	if err := validateSource(p, req.Architecture); err != nil {
		return 0, err
	}
	port, err := servingPort(p)
	if err != nil {
		return 0, err
	}
	return port, validateBindings(p, b.Config.Spec.Secrets, settings.RequiredBindings, req.ProvidedBindings)
}

// validateBindings makes missing lifecycle and runner inputs actionable before packaging.
func validateBindings(p map[string]any, secrets []engine.SecretBinding, required, provided []string) error {
	for _, s := range secrets {
		if !slices.Contains(provided, s.EnvironmentVariable) && !bound(p, s.EnvironmentVariable, false) {
			return fmt.Errorf("bind the Harnest secret %s in .hostlier", s.EnvironmentVariable)
		}
	}
	for _, key := range required {
		if !slices.Contains(provided, key) && !bound(p, key, true) {
			return fmt.Errorf("missing required .hostlier binding %s", key)
		}
	}
	return nil
}

// bound allows credentials only from declared secrets or dependency outputs. Ordinary settings may also use env.
func bound(p map[string]any, key string, allowEnv bool) bool {
	workflow, _ := p["workflow"].(map[string]any)
	for _, v := range []any{p["secrets"], p["bindings"], workflow["bindings"]} {
		m, _ := v.(map[string]any)
		if _, ok := m[key]; ok {
			return true
		}
	}
	env, _ := p["env"].(map[string]any)
	return allowEnv && env[key] != nil
}

// readTopology preserves the authored Hostlier document and defaults only when none exists.
func readTopology(root, name string) (map[string]any, error) {
	_, err := os.Lstat(filepath.Join(root, ".hostlier"))
	if os.IsNotExist(err) {
		return map[string]any{"version": 2, "name": name, "endpoint": map[string]any{"exposure": "public", "port": 1907}, "health": map[string]any{"path": "/healthz"}}, nil
	}
	file, err := openProjectFile(root, ".hostlier")
	if err != nil {
		return nil, err
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, (1<<20)+1))
	if err != nil || len(data) > 1<<20 {
		return nil, errors.New("invalid .hostlier document size")
	}
	var p map[string]any
	decoder := yaml.NewDecoder(bytes.NewReader(data))
	if decoder.Decode(&p) != nil || decoder.Decode(new(any)) != io.EOF || p == nil || p["version"] != 2 {
		return nil, errors.New("expected one Hostlier v2 project")
	}
	return defaultProject(p, name), nil
}

// defaultProject supplies the agent identity and listener for a newly enrolled project.
func defaultProject(p map[string]any, name string) map[string]any {
	if _, ok := p["name"]; !ok {
		p["name"] = name
	}
	if p["endpoint"] == nil && p["endpoints"] == nil {
		p["endpoint"] = map[string]any{"exposure": "public", "port": 1907}
	}
	return p
}

// normalizeTopology converts stored references to Hostlier wire names and excludes project-local package selection.
func normalizeTopology(p map[string]any) error {
	if value := p["package"]; value != nil && value != "harnest" {
		return errors.New(".hostlier selects another package")
	}
	delete(p, "package")
	// Local package settings never enter the deployable workload, including settings owned by other packages.
	delete(p, "packages")
	if err := storedSecrets(p); err != nil {
		return err
	}
	members, _ := p["sidecars"].(map[string]any)
	for _, value := range members {
		member, ok := value.(map[string]any)
		if !ok {
			return errors.New("invalid Hostlier sidecar")
		}
		if err := storedSecrets(member); err != nil {
			return err
		}
	}
	return nil
}

// storedSecrets rejects inline and environment-sourced secrets before serializing any project to stdout.
func storedSecrets(p map[string]any) error {
	if p["secrets"] == nil {
		return nil
	}
	secrets, ok := p["secrets"].(map[string]any)
	if !ok {
		return errors.New("expected Hostlier secret references")
	}
	for key, value := range secrets {
		ref, ok := value.(string)
		name, stored := strings.CutPrefix(ref, "secret://")
		if !ok || !stored || name == "" {
			return errors.New("use stored secret:// references in .hostlier")
		}
		secrets[key] = name
	}
	return nil
}

// validateSource rejects source replacements that would drop the author's image, archive, or custom recipe.
func validateSource(p map[string]any, arch string) error {
	desired := map[string]any{"container": map[string]any{"context": ".", "architecture": arch}}
	if p["image"] != nil || p["profile"] != nil {
		return errors.New("the Harnest package owns the primary container source and entrypoint")
	}
	if source := p["source"]; source != nil && !reflect.DeepEqual(source, desired) && !reflect.DeepEqual(source, map[string]any{"container": map[string]any{"context": "."}}) {
		return errors.New("use source.container.context '.' for the Harnest package")
	}
	p["source"] = desired
	return nil
}

// primaryEndpoint follows Hostlier v2's explicit, conventional, then lexical primary listener selection.
func primaryEndpoint(p map[string]any) map[string]any {
	endpoints, _ := p["endpoints"].(map[string]any)
	if len(endpoints) == 0 {
		endpoint, _ := p["endpoint"].(map[string]any)
		return endpoint
	}
	if name, ok := p["primaryEndpoint"].(string); ok && name != "" {
		endpoint, _ := endpoints[name].(map[string]any)
		return endpoint
	}
	for _, name := range []string{"http", "main", "default"} {
		if endpoint, ok := endpoints[name].(map[string]any); ok {
			return endpoint
		}
	}
	name := slices.Sorted(maps.Keys(endpoints))[0]
	endpoint, _ := endpoints[name].(map[string]any)
	return endpoint
}

// servingPort uses the primary endpoint's port for the agent HTTP process.
func servingPort(p map[string]any) (int, error) {
	port, ok := primaryEndpoint(p)["port"].(int)
	if !ok || port < 1 || port > 65535 {
		return 0, errors.New("declare a valid Hostlier primary endpoint for the Harnest HTTP listener")
	}
	return port, nil
}

// sameWorkload checks the inputs consumed by the image builder. Hostlier owns route overrides
// and may normalize unconsumed fields, including the defaults of nested sidecars.
func sameWorkload(reviewed, current map[string]any) bool {
	reviewedEndpoint := primaryEndpoint(reviewed)
	endpoint := primaryEndpoint(current)
	return reviewed["name"] == current["name"] &&
		fmt.Sprint(reviewedEndpoint["port"]) == fmt.Sprint(endpoint["port"]) &&
		reflect.DeepEqual(reviewed["source"], current["source"])
}
