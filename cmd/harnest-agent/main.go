// harnest-agent is the dependency-free native launcher embedded in agent executables.
package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"harnest.dev/harnest/internal/agentpack"
)

// main preserves the agent's exit status without requiring the Harnest CLI at runtime.
func main() {
	code, err := dispatch(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, "harnest-agent:", err)
	}
	os.Exit(code)
}

// dispatch shares process lifecycle handling between agents and packaged console tools.
func dispatch(args []string) (int, error) {
	filename, err := os.Executable()
	if err != nil {
		return 1, err
	}
	tool, err := agentpack.ConsoleCommand(filename, args)
	if err != nil {
		return 1, err
	}
	if tool != nil {
		tool.Stdin, tool.Stdout, tool.Stderr = os.Stdin, os.Stdout, os.Stderr
		return waitAgent(tool)
	}
	if helpRequested(args) {
		fmt.Println("Usage: agent [--runtime PATH] [--server-config FILE] serve|run [OPTIONS]\n\n--runtime PATH        Attach the exact runtime pack used at compilation\n--server-config FILE Override packaged server settings for this process\n\nUse HARNEST_AGENT_RUNTIME for a default attachment and HARNEST_AGENT_CACHE\nto choose the shared runtime cache. Pass serve --help or run --help for command options.")
		return 0, nil
	}
	return launch(args)
}

type options struct {
	runtime, server string
	args            []string
}

// parseOptions owns only launcher flags before the runtime subcommand.
func parseOptions(args []string) (options, error) {
	result := options{runtime: os.Getenv("HARNEST_AGENT_RUNTIME")}
	for len(args) > 0 {
		name, value, equal := strings.Cut(args[0], "=")
		if name != "--runtime" && name != "--server-config" {
			break
		}
		args = args[1:]
		if !equal {
			if len(args) == 0 {
				return result, fmt.Errorf("%s requires a path", name)
			}
			value, args = args[0], args[1:]
		}
		if value == "" {
			return result, fmt.Errorf("%s requires a path", name)
		}
		if name == "--runtime" {
			result.runtime = value
		} else {
			result.server = value
		}
	}
	result.args = args
	return result, nil
}

// launch verifies the attachment before importing any agent or dependency code.
func launch(args []string) (int, error) {
	opts, err := parseOptions(args)
	if err != nil {
		return 1, err
	}
	filename, err := os.Executable()
	if err != nil {
		return 1, err
	}
	archive, metadata, err := agentpack.ReadExecutable(filename)
	if err != nil {
		return 1, err
	}
	defer archive.Close()
	if err = metadata.Runtime.CheckHost(); err != nil {
		return 1, err
	}
	temporary, err := os.MkdirTemp("", "harnest-agent-*")
	if err != nil {
		return 1, err
	}
	defer os.RemoveAll(temporary)
	pack, err := extractAttachedRuntime(archive, metadata, opts.runtime, temporary)
	if err != nil {
		return 1, err
	}
	root, err := attachRuntime(pack, metadata.Runtime)
	if err != nil {
		return 1, err
	}
	artifact := filepath.Join(temporary, "agent")
	if err = agentpack.ExtractPrefix(archive, "agent", artifact); err != nil {
		return 1, err
	}
	if err = overrideServer(opts.server, artifact); err != nil {
		return 1, err
	}
	return runAgent(root, artifact, opts.args, metadata.Environment)
}

// extractAttachedRuntime expands bundled objects only when no external attachment was selected.
func extractAttachedRuntime(archive *agentpack.Archive, metadata agentpack.Executable, selected, temporary string) (string, error) {
	pack := selected
	if pack == "" && metadata.Embedded {
		pack = filepath.Join(temporary, "runtime")
		if err := agentpack.ExtractPrefix(archive, "runtime", pack); err != nil {
			return "", err
		}
	}
	if pack == "" {
		return "", fmt.Errorf("attach runtime %s with --runtime PATH or HARNEST_AGENT_RUNTIME", metadata.Runtime.Digest)
	}
	return pack, nil
}

// attachRuntime compares complete identities, including platform and dependency inputs.
func attachRuntime(pack string, expected agentpack.Reference) (string, error) {
	actual, err := agentpack.ReadManifest(pack)
	if err != nil {
		return "", err
	}
	if actual.Digest != expected.Digest {
		return "", fmt.Errorf("attached runtime %s does not match required %s", actual.Digest, expected.Digest)
	}
	cache, err := agentpack.CacheDirectory(os.Getenv("HARNEST_AGENT_CACHE"), os.UserCacheDir)
	if err != nil {
		return "", err
	}
	return agentpack.Materialize(pack, cache, actual)
}

// overrideServer leaves the immutable executable untouched when deployment policy changes.
func overrideServer(source, artifact string) error {
	if source == "" {
		return nil
	}
	data, err := os.ReadFile(source)
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(artifact, "server.yaml"), data, 0600)
}

// runAgent connects terminal streams and leaves writable application state outside the cache.
func runAgent(root, artifact string, args []string, defaults map[string]string) (int, error) {
	m, err := agentpack.ReadManifest(root)
	if err != nil {
		return 1, err
	}
	python, argv := agentpack.PythonCommand(root, m, "harnest.runtime", append([]string{"--artifact", artifact}, args...))
	command := exec.Command(python, argv...)
	command.Env = agentpack.Environment(root, m)
	for key, value := range defaults {
		if _, exists := os.LookupEnv(key); !exists && allowedDefault(key) {
			command.Env = append(command.Env, key+"="+value)
		}
	}
	command.Stdin, command.Stdout, command.Stderr = os.Stdin, os.Stdout, os.Stderr
	return waitAgent(command)
}

// allowedDefault reserves interpreter isolation variables for the launcher.
func allowedDefault(key string) bool {
	upper := strings.ToUpper(key)
	return upper != "PATH" && upper != "VIRTUAL_ENV" && !strings.HasPrefix(upper, "PYTHON")
}

// helpRequested keeps attachment instructions available before a runtime is installed.
func helpRequested(args []string) bool {
	return len(args) == 1 && (args[0] == "--help" || args[0] == "-h")
}
