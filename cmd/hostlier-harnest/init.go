// Proposes starter agent files and their Hostlier workload settings.
package main

import (
	"bytes"
	"cmp"
	"encoding/json"
	"errors"
	"io"
	"os"

	"harnest.dev/harnest/internal/agentscaffold"
)

// initRequest carries Hostlier’s public starter settings. Package-specific inputs arrive under config.
type initRequest struct {
	Version      int            `json:"version"`
	UI           bool           `json:"ui,omitempty"`
	Name         string         `json:"name,omitempty"`
	DefaultName  string         `json:"defaultName"`
	Port         int            `json:"port,omitempty"`
	Route        string         `json:"route,omitempty"`
	Architecture string         `json:"architecture,omitempty"`
	Config       map[string]any `json:"config,omitempty"`
}

type initResponse struct {
	Version int               `json:"version"`
	Project map[string]any    `json:"project,omitempty"`
	Files   map[string]string `json:"files,omitempty"`
	Error   string            `json:"error,omitempty"`
}

// dispatchInit proposes the same files for dry runs and live runs. Hostlier owns all project writes.
func dispatchInit(in io.Reader, out io.Writer) error {
	data, err := io.ReadAll(io.LimitReader(in, (4<<20)+1))
	var req initRequest
	decoder := json.NewDecoder(bytes.NewReader(data))
	if err != nil || len(data) > 4<<20 || decoder.Decode(&req) != nil || decoder.Decode(new(any)) != io.EOF || req.Version != 1 {
		return errors.New("invalid Harnest init request")
	}
	response, err := proposeInit(req)
	if err != nil {
		response = initResponse{Error: err.Error()}
	}
	response.Version = 1
	return json.NewEncoder(out).Encode(response)
}

// initAgentName validates inline build choices and resolves a single identity for source and deployment.
func initAgentName(req initRequest) (string, error) {
	configured, err := starterName(req.Config)
	if err != nil {
		return "", err
	}
	if configured != "" && req.Name != "" && configured != req.Name {
		return "", errors.New("--name must match packages.harnest.name")
	}
	name := cmp.Or(configured, req.Name, req.DefaultName)
	if !agentName.MatchString(name) {
		return "", errors.New("use a lowercase agent name starting with a letter (maximum 63 characters)")
	}
	return name, nil
}

// starterName allows initialization before build settings are filled and validates any provided choices.
func starterName(fields map[string]any) (string, error) {
	if len(fields) == 0 {
		return "", nil
	}
	if value, ok := fields["name"]; ok && len(fields) == 1 {
		name, ok := value.(string)
		if !ok || !agentName.MatchString(name) {
			return "", errors.New("invalid packages.harnest.name")
		}
		return name, nil
	}
	settings, err := inlineSettings(fields)
	return settings.Name, err
}

// proposeInit creates an ordinary managed ADK starter without building, installing dependencies or resolving secrets.
func proposeInit(req initRequest) (initResponse, error) {
	var response initResponse
	if req.UI {
		return response, errors.New("the Harnest initializer does not provide a UI template")
	}
	name, err := initAgentName(req)
	if err != nil {
		return response, err
	}
	arch := cmp.Or(req.Architecture, "amd64")
	if arch != "amd64" && arch != "arm64" {
		return response, errors.New("unsupported Hostlier architecture")
	}
	port := cmp.Or(req.Port, 1907)
	if port < 1 || port > 65535 {
		return response, errors.New("invalid Harnest listening port")
	}
	response.Project = map[string]any{
		"version": 2, "name": name,
		"source":   map[string]any{"container": map[string]any{"context": ".", "architecture": arch}},
		"endpoint": map[string]any{"exposure": "public", "port": port, "route": req.Route},
		"health":   map[string]any{"path": "/healthz"},
	}
	response.Files = agentscaffold.ManagedFiles(name, "adk", port)
	// An existing ignore policy belongs to the project, even when it is a symlink.
	if _, err := os.Lstat(".gitignore"); err == nil {
		delete(response.Files, ".gitignore")
	} else if !os.IsNotExist(err) {
		return initResponse{}, errors.New("cannot inspect existing .gitignore")
	}
	return response, nil
}
