// Package engine implements the Go-side runtime contract for Harnest agents.
package engine

import "time"

const APIVersion = "harnest.dev/v1alpha1"

type DeploymentPlan struct {
	APIVersion  string            `json:"apiVersion"`
	Kind        string            `json:"kind"`
	ProjectRoot string            `json:"projectRoot"`
	Parallelism int               `json:"parallelism"`
	FailFast    bool              `json:"failFast"`
	Labels      map[string]string `json:"labels,omitempty"`
	Sources     []AgentSource     `json:"sources"`
}

type AgentSource struct {
	Root    string   `json:"root"`
	Include []string `json:"include,omitempty"`
	Exclude []string `json:"exclude,omitempty"`
}

type AgentConfig struct {
	APIVersion string          `yaml:"apiVersion" json:"apiVersion"`
	Kind       string          `yaml:"kind" json:"kind"`
	Metadata   AgentMetadata   `yaml:"metadata" json:"metadata"`
	Spec       AgentConfigSpec `yaml:"spec" json:"spec"`
}

type AgentMetadata struct {
	Name        string            `yaml:"name" json:"name"`
	DisplayName string            `yaml:"displayName,omitempty" json:"displayName,omitempty"`
	Labels      map[string]string `yaml:"labels,omitempty" json:"labels,omitempty"`
}

type AgentConfigSpec struct {
	Enabled     *bool             `yaml:"enabled,omitempty" json:"enabled,omitempty"`
	Entrypoint  string            `yaml:"entrypoint" json:"entrypoint"`
	Framework   AgentFramework    `yaml:"framework" json:"framework"`
	Runtime     PythonRuntime     `yaml:"runtime" json:"runtime"`
	Resources   AgentResources    `yaml:"resources" json:"resources"`
	Scaling     Scaling           `yaml:"scaling,omitempty" json:"scaling,omitempty"`
	Environment map[string]string `yaml:"environment,omitempty" json:"environment,omitempty"`
	Secrets     []SecretBinding   `yaml:"secrets,omitempty" json:"secrets,omitempty"`
	Permissions Permissions       `yaml:"permissions,omitempty" json:"permissions,omitempty"`
}

type AgentFramework struct {
	Name string `yaml:"name" json:"name"`
	Mode string `yaml:"mode,omitempty" json:"mode,omitempty"`
}

func (f AgentFramework) EffectiveMode() string {
	if f.Mode == "" {
		return "managed"
	}
	return f.Mode
}

func (s AgentConfigSpec) IsEnabled() bool { return s.Enabled == nil || *s.Enabled }

type PythonRuntime struct {
	Version          string `yaml:"version" json:"version"`
	RequirementsFile string `yaml:"requirementsFile,omitempty" json:"requirementsFile,omitempty"`
}

type AgentResources struct {
	CPU                   string `yaml:"cpu" json:"cpu"`
	Memory                string `yaml:"memory" json:"memory"`
	EphemeralStorage      string `yaml:"ephemeralStorage,omitempty" json:"ephemeralStorage,omitempty"`
	TimeoutSeconds        int    `yaml:"timeoutSeconds,omitempty" json:"timeoutSeconds,omitempty"`
	MaxConcurrentRequests int    `yaml:"maxConcurrentRequests,omitempty" json:"maxConcurrentRequests,omitempty"`
}

type Scaling struct {
	MinReplicas int `yaml:"minReplicas,omitempty" json:"minReplicas,omitempty"`
	MaxReplicas int `yaml:"maxReplicas,omitempty" json:"maxReplicas,omitempty"`
}

type SecretBinding struct {
	EnvironmentVariable string `yaml:"environmentVariable" json:"environmentVariable"`
	SecretRef           string `yaml:"secretRef" json:"secretRef"`
}

type Permissions struct {
	Network    NetworkPermissions    `yaml:"network,omitempty" json:"network,omitempty"`
	Filesystem FilesystemPermissions `yaml:"filesystem,omitempty" json:"filesystem,omitempty"`
}

type NetworkPermissions struct {
	Outbound []string `yaml:"outbound,omitempty" json:"outbound,omitempty"`
}

type FilesystemPermissions struct {
	ReadOnly  []string `yaml:"readOnly,omitempty" json:"readOnly,omitempty"`
	ReadWrite []string `yaml:"readWrite,omitempty" json:"readWrite,omitempty"`
}

// AgentCard follows the required core of A2A Agent Card 1.0. YAML is used as
// the authoring format; deployers can serialize the same value as JSON.
type AgentCard struct {
	Name                string            `yaml:"name" json:"name"`
	Description         string            `yaml:"description" json:"description"`
	Version             string            `yaml:"version" json:"version"`
	Provider            *AgentProvider    `yaml:"provider,omitempty" json:"provider,omitempty"`
	DocumentationURL    string            `yaml:"documentationUrl,omitempty" json:"documentationUrl,omitempty"`
	SupportedInterfaces []AgentInterface  `yaml:"supportedInterfaces" json:"supportedInterfaces"`
	Capabilities        AgentCapabilities `yaml:"capabilities" json:"capabilities"`
	DefaultInputModes   []string          `yaml:"defaultInputModes" json:"defaultInputModes"`
	DefaultOutputModes  []string          `yaml:"defaultOutputModes" json:"defaultOutputModes"`
	Skills              []AgentSkill      `yaml:"skills" json:"skills"`
}

type AgentProvider struct {
	Organization string `yaml:"organization" json:"organization"`
	URL          string `yaml:"url" json:"url"`
}

type AgentInterface struct {
	URL             string `yaml:"url" json:"url"`
	ProtocolBinding string `yaml:"protocolBinding" json:"protocolBinding"`
	ProtocolVersion string `yaml:"protocolVersion" json:"protocolVersion"`
}

type AgentCapabilities struct {
	Streaming         bool `yaml:"streaming,omitempty" json:"streaming,omitempty"`
	PushNotifications bool `yaml:"pushNotifications,omitempty" json:"pushNotifications,omitempty"`
	ExtendedAgentCard bool `yaml:"extendedAgentCard,omitempty" json:"extendedAgentCard,omitempty"`
}

type AgentSkill struct {
	ID          string   `yaml:"id" json:"id"`
	Name        string   `yaml:"name" json:"name"`
	Description string   `yaml:"description" json:"description"`
	Tags        []string `yaml:"tags" json:"tags"`
	Examples    []string `yaml:"examples,omitempty" json:"examples,omitempty"`
	InputModes  []string `yaml:"inputModes,omitempty" json:"inputModes,omitempty"`
	OutputModes []string `yaml:"outputModes,omitempty" json:"outputModes,omitempty"`
}

type Bundle struct {
	Directory        string
	ConfigPath       string
	CardPath         string
	InstructionsPath string
	Config           AgentConfig
	Card             AgentCard
	Labels           map[string]string
	Digest           string
	Compiled         *CompiledArtifact
	LoadedAt         time.Time
}

type CompiledArtifact struct {
	Directory    string
	ManifestPath string
	Manifest     CompiledManifest
}

type CompiledManifest struct {
	APIVersion       string            `json:"apiVersion"`
	Kind             string            `json:"kind"`
	Name             string            `json:"name"`
	Entrypoint       string            `json:"entrypoint"`
	SourceEntrypoint string            `json:"sourceEntrypoint"`
	SourceDirectory  string            `json:"sourceDirectory"`
	HarnestVersion   string            `json:"harnestVersion"`
	Framework        CompiledFramework `json:"framework"`
	Digest           string            `json:"digest"`
	Files            []CompiledFile    `json:"files"`
}

type CompiledFramework struct {
	Name         string `json:"name"`
	Mode         string `json:"mode"`
	Distribution string `json:"distribution"`
	Version      string `json:"version"`
}

func (f CompiledFramework) EffectiveMode() string {
	if f.Mode == "" {
		return "managed"
	}
	return f.Mode
}

type CompiledFile struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}
