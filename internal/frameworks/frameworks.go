// Package frameworks exposes the release-owned agent framework metadata shared
// with the Python compiler.
//
// The canonical file is src/harnest/frameworks/frameworks.json. This package
// embeds a byte-identical copy (Go can only embed files below its own
// directory); TestEmbeddedMetadataMatchesPythonSource fails when they drift.
// Adding a framework starts with one entry in that file.
package frameworks

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
)

// Capability names optional behavior a framework declares.
type Capability string

// Capabilities shared with harnest.frameworks.base.Capability.
const (
	Graphs               Capability = "graphs"
	NativeAPIServer      Capability = "framework-api-server"
	NativeSessionService Capability = "framework-session-service"
	RootAgentSubagents   Capability = "root-agent-subagents"
	Evals                Capability = "evals"
	// GenerateContentConfig marks frameworks whose models use Agent(generate_content_config=...).
	GenerateContentConfig Capability = "generate-content-config"
)

// DoctorModule is one importable module `harnest doctor` checks.
type DoctorModule struct {
	Module       string `json:"module"`
	Distribution string `json:"distribution"`
}

// Framework is the metadata for one supported agent framework.
type Framework struct {
	Name            string         `json:"name"`
	DisplayName     string         `json:"display_name"`
	Distribution    string         `json:"distribution"`
	Versions        string         `json:"versions"`
	InstallExtra    string         `json:"install_extra"`
	Requirements    []string       `json:"requirements"`
	MCPRequirements []string       `json:"mcp_requirements"`
	DoctorModules   []DoctorModule `json:"doctor_modules"`
	Capabilities    []Capability   `json:"capabilities"`
}

type document struct {
	SchemaVersion             int         `json:"schema_version"`
	SharedRuntimeRequirements []string    `json:"shared_runtime_requirements"`
	Frameworks                []Framework `json:"frameworks"`
}

//go:embed frameworks.json
var embedded []byte

var metadata = mustParse(embedded)

// mustParse decodes the embedded document; a malformed release file is a
// build defect, so it fails at process start rather than per command.
func mustParse(data []byte) document {
	var parsed document
	if err := json.Unmarshal(data, &parsed); err != nil {
		panic(fmt.Sprintf("invalid embedded frameworks.json: %v", err))
	}
	return parsed
}

// All returns every registered framework in stable CLI order.
func All() []Framework {
	return slices.Clone(metadata.Frameworks)
}

// Names returns registered framework names in stable CLI order.
func Names() []string {
	names := make([]string, 0, len(metadata.Frameworks))
	for _, framework := range metadata.Frameworks {
		names = append(names, framework.Name)
	}
	return names
}

// Choices renders the registered names for flags and errors, e.g. "adk or langgraph".
func Choices() string {
	return strings.Join(Names(), " or ")
}

// Lookup returns one registered framework by name.
func Lookup(name string) (Framework, bool) {
	for _, framework := range metadata.Frameworks {
		if framework.Name == name {
			return framework, true
		}
	}
	return Framework{}, false
}

// Known reports whether name is a registered framework.
func Known(name string) bool {
	_, ok := Lookup(name)
	return ok
}

// Supports reports whether the framework declares one optional capability.
func (f Framework) Supports(capability Capability) bool {
	return slices.Contains(f.Capabilities, capability)
}

// RuntimeRequirements returns the framework's packages plus the store drivers
// every compiled runtime installs.
func (f Framework) RuntimeRequirements() []string {
	requirements := slices.Clone(f.Requirements)
	return append(requirements, metadata.SharedRuntimeRequirements...)
}
