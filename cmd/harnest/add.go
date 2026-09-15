package main

import (
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/engine"
)

var agentResourceNamePattern = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,62}$`)
var environmentVariableNamePattern = regexp.MustCompile(`^[A-Z_][A-Z0-9_]*$`)
var httpHeaderNamePattern = regexp.MustCompile("^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

var pythonReservedNames = map[string]bool{
	"and": true, "as": true, "assert": true, "async": true, "await": true,
	"break": true, "class": true, "continue": true, "def": true, "del": true,
	"elif": true, "else": true, "except": true, "finally": true, "for": true,
	"from": true, "global": true, "if": true, "import": true, "in": true,
	"is": true, "lambda": true, "nonlocal": true, "not": true, "or": true,
	"pass": true, "raise": true, "return": true, "try": true, "while": true,
	"with": true, "yield": true,
}

type agentResourceScaffold struct {
	Kind        string
	Directory   string
	Description string
	ManagedOnly bool
	ADKOnly     bool
	Source      func(string) string
	Next        func(string) string
}

type addMCPOptions struct {
	Project     string
	URL         string
	Transport   string
	TokenEnv    string
	TokenHeader string
	TokenPrefix string
}

// newAddCommand groups incremental resource scaffolds for minimal agents.
func (a *application) newAddCommand() *cobra.Command {
	command := &cobra.Command{
		Use:   "add",
		Short: "Add one capability to an existing Harnest agent",
		Long: `Add one named, compiler-valid resource without replacing authored files.

Start with harnest init <agent> --minimal, then add only the capabilities the
agent needs. Harnest recreates a deleted optional folder when required. Use the
dedicated extensions, plugins, and skills commands for packaged resources.`,
	}
	for _, scaffold := range agentResourceScaffolds() {
		command.AddCommand(a.newAddResourceCommand(scaffold))
	}
	command.AddCommand(a.newAddMCPCommand())
	return command
}

// newAddMCPCommand scaffolds one remote connection without accepting secret values.
func (a *application) newAddMCPCommand() *cobra.Command {
	options := addMCPOptions{}
	command := &cobra.Command{
		Use:   "mcp NAME",
		Short: "Add a remote MCP client connection",
		Long: `Add a Streamable HTTP or legacy SSE MCP connection to a managed agent.

The command stores only an environment-variable reference, never a token value.
Choose the destination header and token prefix to match the server's auth scheme.`,
		Example: `  harnest add mcp catalog --url https://mcp.example.com/mcp --token-env CATALOG_MCP_TOKEN
  harnest add mcp internal --url https://mcp.example.com/mcp --token-env INTERNAL_KEY --token-header X-API-Key --token-prefix=`,
		Args: cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			if err := options.validate(
				command.Flags().Changed("token-header"),
				command.Flags().Changed("token-prefix"),
			); err != nil {
				return err
			}
			created, normalized, err := addMCPResource(
				options, arguments[0],
			)
			if err != nil {
				return err
			}
			fmt.Fprintf(command.OutOrStdout(), "Added mcp %q at %s\n", normalized, created)
			if options.TokenEnv == "" {
				fmt.Fprintln(command.OutOrStdout(), "Next: run `harnest test .` from the agent folder")
			} else {
				fmt.Fprintf(
					command.OutOrStdout(),
					"Next: export %s, then run `harnest test .` from the agent folder\n",
					options.TokenEnv,
				)
			}
			return nil
		},
	}
	flags := command.Flags()
	flags.StringVar(&options.Project, "project", ".", "Harnest agent root containing config.yaml")
	flags.StringVar(&options.URL, "url", "", "remote MCP server URL")
	flags.StringVar(&options.Transport, "transport", "streamable-http", "HTTP transport: streamable-http or sse")
	flags.StringVar(&options.TokenEnv, "token-env", "", "environment variable containing the token")
	flags.StringVar(&options.TokenHeader, "token-header", "Authorization", "HTTP header that receives the token")
	flags.StringVar(&options.TokenPrefix, "token-prefix", "Bearer ", "text placed before the token; use --token-prefix= for a raw token")
	return command
}

// validate rejects unsafe or ineffective remote MCP settings before filesystem writes.
func (options addMCPOptions) validate(headerChanged, prefixChanged bool) error {
	if err := validateRemoteMCPURL(options.URL); err != nil {
		return err
	}
	if options.Transport != "streamable-http" && options.Transport != "sse" {
		return fmt.Errorf("--transport must be streamable-http or sse")
	}
	return options.validateToken(headerChanged, prefixChanged)
}

// validateToken keeps auth settings explicit while allowing public MCP servers.
func (options addMCPOptions) validateToken(headerChanged, prefixChanged bool) error {
	if options.TokenEnv == "" {
		if headerChanged || prefixChanged {
			return fmt.Errorf("--token-header and --token-prefix require --token-env")
		}
		return nil
	}
	if !environmentVariableNamePattern.MatchString(options.TokenEnv) {
		return fmt.Errorf("--token-env must name an uppercase environment variable")
	}
	if !httpHeaderNamePattern.MatchString(options.TokenHeader) {
		return fmt.Errorf("--token-header must be a valid HTTP header name")
	}
	if len(options.TokenPrefix) > 128 || strings.ContainsAny(options.TokenPrefix, "\r\n") {
		return fmt.Errorf("--token-prefix must be at most 128 characters without newlines")
	}
	return nil
}

// validateRemoteMCPURL permits explicit HTTP transports without embedded credentials.
func validateRemoteMCPURL(raw string) error {
	if raw == "" || raw != strings.TrimSpace(raw) {
		return fmt.Errorf("--url must be a non-empty HTTP or HTTPS URL")
	}
	parsed, err := url.ParseRequestURI(raw)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return fmt.Errorf("--url must be a valid HTTP or HTTPS URL")
	}
	if parsed.User != nil {
		return fmt.Errorf("--url must not contain credentials; use --token-env")
	}
	return nil
}

// addMCPResource reuses the standard project, mode, path, and collision policy.
func addMCPResource(
	options addMCPOptions, requestedName string,
) (string, string, error) {
	scaffold := agentResourceScaffold{
		Kind: "mcp", Directory: "mcp", ManagedOnly: true,
		Source: func(name string) string {
			return mcpResourceSource(name, options)
		},
	}
	return addAgentResource(options.Project, requestedName, scaffold)
}

// newAddResourceCommand binds shared project and collision policy to one kind.
func (a *application) newAddResourceCommand(
	scaffold agentResourceScaffold,
) *cobra.Command {
	var project string
	command := &cobra.Command{
		Use:   scaffold.Kind + " NAME",
		Short: scaffold.Description,
		Args:  cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			created, normalized, err := addAgentResource(
				project, arguments[0], scaffold,
			)
			if err != nil {
				return err
			}
			fmt.Fprintf(
				command.OutOrStdout(),
				"Added %s %q at %s\n",
				scaffold.Kind,
				normalized,
				created,
			)
			if scaffold.Next != nil {
				fmt.Fprintf(command.OutOrStdout(), "Next: %s\n", scaffold.Next(normalized))
			}
			return nil
		},
	}
	command.Flags().StringVar(
		&project, "project", ".", "Harnest agent root containing config.yaml",
	)
	return command
}

// addAgentResource validates project ownership before creating one public file.
func addAgentResource(
	project, requestedName string,
	scaffold agentResourceScaffold,
) (string, string, error) {
	root, err := validatedAgentProject(project)
	if err != nil {
		return "", "", err
	}
	name, err := normalizedAgentResourceName(requestedName)
	if err != nil {
		return "", "", err
	}
	if err := validateAgentResourceCompatibility(root, scaffold); err != nil {
		return "", "", err
	}
	created, err := createAgentResourceFile(root, scaffold, name)
	if err != nil {
		return "", "", err
	}
	return created, name, nil
}

// normalizedAgentResourceName maps CLI kebab-case to one safe Python export.
func normalizedAgentResourceName(requested string) (string, error) {
	if requested != strings.TrimSpace(requested) || !agentResourceNamePattern.MatchString(requested) {
		return "", fmt.Errorf(
			"resource name %q must begin with a lowercase letter and contain only lowercase letters, numbers, hyphens, or underscores",
			requested,
		)
	}
	name := strings.ReplaceAll(requested, "-", "_")
	if pythonReservedNames[name] {
		return "", fmt.Errorf("resource name %q is reserved by Python", requested)
	}
	return name, nil
}

// validateAgentResourceCompatibility prevents scaffolding ignored or invalid code.
func validateAgentResourceCompatibility(
	root string, scaffold agentResourceScaffold,
) error {
	bundle, err := engine.LoadBundle(root)
	if err != nil {
		return fmt.Errorf("load Harnest agent before adding %s: %w", scaffold.Kind, err)
	}
	framework := bundle.Config.Spec.Framework.Name
	mode := bundle.Config.Spec.Framework.EffectiveMode()
	if scaffold.ManagedOnly && mode != "managed" {
		return fmt.Errorf(
			"harnest add %s requires managed mode; advanced mode owns this wiring in agent.py",
			scaffold.Kind,
		)
	}
	if scaffold.ADKOnly && framework != "adk" {
		return fmt.Errorf(
			"harnest add %s currently requires the adk framework; LangGraph subagents must be explicit Graph nodes in agent.py",
			scaffold.Kind,
		)
	}
	return nil
}

// createAgentResourceFile creates an absent optional directory without overwriting.
func createAgentResourceFile(
	root string, scaffold agentResourceScaffold, name string,
) (created string, returnErr error) {
	directory := filepath.Join(root, scaffold.Directory)
	createdDirectory, err := prepareAgentResourceDirectory(directory, scaffold.Directory)
	if err != nil {
		return "", err
	}
	if createdDirectory {
		defer func() {
			if returnErr != nil {
				_ = os.Remove(directory)
			}
		}()
	}
	destination := filepath.Join(directory, name+".py")
	if _, err := os.Lstat(destination); err == nil {
		return "", fmt.Errorf("%s %q already exists at %s", scaffold.Kind, name, destination)
	} else if !os.IsNotExist(err) {
		return "", fmt.Errorf("inspect %s destination: %w", scaffold.Kind, err)
	}
	if err := createScaffoldFile(destination, scaffold.Source(name)); err != nil {
		return "", err
	}
	return destination, nil
}

// prepareAgentResourceDirectory rejects linked roots before optional recreation.
func prepareAgentResourceDirectory(path, label string) (bool, error) {
	if err := validateLocalPackageRoot(path, label); err != nil {
		return false, err
	}
	if _, err := os.Lstat(path); err == nil {
		return false, nil
	} else if !os.IsNotExist(err) {
		return false, fmt.Errorf("inspect target %s directory: %w", label, err)
	}
	if err := os.Mkdir(path, 0o755); err != nil {
		return false, fmt.Errorf("create target %s directory: %w", label, err)
	}
	return true, nil
}

// agentResourceScaffolds defines the intentionally small incremental surface.
func agentResourceScaffolds() []agentResourceScaffold {
	return []agentResourceScaffold{
		{
			Kind: "tool", Directory: "tools", ManagedOnly: true,
			Description: "Add a managed model tool",
			Source:      toolResourceSource,
			Next: func(string) string {
				return "implement the tool body and run `harnest test .` from the agent folder"
			},
		},
		{
			Kind: "subagent", Directory: "subagents", ManagedOnly: true, ADKOnly: true,
			Description: "Add a managed ADK subagent",
			Source:      subagentResourceSource,
			Next: func(string) string {
				return "customize its instruction; managed ADK discovers and attaches it automatically"
			},
		},
		{
			Kind: "task", Directory: "tasks",
			Description: "Add a durable application task",
			Source:      taskResourceSource,
			Next: func(string) string {
				return "implement durable work and configure task storage before using `.defer(...)`"
			},
		},
		{
			Kind: "lifecycle", Directory: "lifecycle",
			Description: "Add an application lifecycle hook",
			Source:      lifecycleResourceSource,
			Next: func(string) string {
				return "choose the grouped lifecycle phase that owns this hook"
			},
		},
		{
			Kind: "context", Directory: "lifecycle",
			Description: "Add an invocation-scoped context provider",
			Source:      contextResourceSource,
			Next: func(name string) string {
				return fmt.Sprintf("resolve the value during invocation with `context.resource(%q)`", name)
			},
		},
	}
}

// toolResourceSource returns a safe managed tool that authors can refine.
func toolResourceSource(name string) string {
	return fmt.Sprintf(`from harnest.agent import tool


@tool
def %s(request: str) -> str:
    """Handle one %s request."""
    return request
`, name, strings.ReplaceAll(name, "_", " "))
}

// subagentResourceSource keeps delegated agents on the shared model configuration as their root.
func subagentResourceSource(name string) string {
	return fmt.Sprintf(`from harnest.agent import Agent
from harnest.model import LiteLLMModel


%s = Agent(
    name=%q,
    model=LiteLLMModel.from_openai_environment(),
    description=%q,
    instruction=%q,
    history="session",
)
`, name, name, "Handles delegated "+strings.ReplaceAll(name, "_", " ")+" work.",
		"Complete the delegated task clearly and return the result to the parent agent.")
}

// taskResourceSource returns a durable task with a serializable result.
func taskResourceSource(name string) string {
	return fmt.Sprintf(`from harnest.task import task


@task(queue="default", max_retries=3)
async def %s(payload: str) -> dict[str, str]:
    """Perform one durable %s operation."""
    return {"payload": payload, "status": "completed"}
`, name, strings.ReplaceAll(name, "_", " "))
}

// lifecycleResourceSource returns an observational agent completion hook.
func lifecycleResourceSource(name string) string {
	return fmt.Sprintf(`from harnest import lifecycle


@lifecycle.agent.after
def %s(_context, result):
    """Observe a completed invocation without changing its result."""
    return result
`, name)
}

// contextResourceSource returns an explicit invocation-scoped provider.
func contextResourceSource(name string) string {
	return fmt.Sprintf(`from harnest import context


@context.provider(%q)
def %s():
    """Create one invocation-scoped %s value."""
    return {}
`, name, name, strings.ReplaceAll(name, "_", " "))
}

// mcpResourceSource emits only configuration and an environment placeholder.
func mcpResourceSource(name string, options addMCPOptions) string {
	constructor := strings.ReplaceAll(options.Transport, "-", "_")
	headers := ""
	if options.TokenEnv != "" {
		value := options.TokenPrefix + "${" + options.TokenEnv + "}"
		headers = fmt.Sprintf(
			"\n        headers={%s: %s},",
			strconv.Quote(options.TokenHeader),
			strconv.Quote(value),
		)
	}
	return fmt.Sprintf(`"""Connect to the %s MCP server."""

from harnest.mcp import MCPClient


def client() -> MCPClient:
    """Create the remote MCP client from non-secret authored configuration."""

    return MCPClient.%s(
        %s,%s
        prefix=%s,
    )
`, strings.ReplaceAll(name, "_", " "), constructor, strconv.Quote(options.URL), headers, strconv.Quote(name))
}
