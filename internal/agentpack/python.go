package agentpack

import (
	"os"
	"path/filepath"
	"strings"
)

const pythonBootstrap = `import runpy,site,sys; site.addsitedir(sys.argv.pop(1)); runpy.run_module(sys.argv.pop(1),run_name="__main__")`

// PythonCommand runs only the attached interpreter and its explicitly installed packages.
func PythonCommand(root string, m Manifest, module string, args []string) (string, []string) {
	python := filepath.Join(root, filepath.FromSlash(m.Python))
	argv := []string{"-I", "-B", "-c", pythonBootstrap, filepath.Join(root, "packages"), module}
	return python, append(argv, args...)
}

// Environment isolates Python imports while making bundled console tools discoverable.
func Environment(root string, m Manifest) []string {
	var result []string
	for _, entry := range os.Environ() {
		key, _, _ := strings.Cut(entry, "=")
		if strings.HasPrefix(strings.ToUpper(key), "PYTHON") || strings.EqualFold(key, "PATH") || strings.EqualFold(key, "VIRTUAL_ENV") {
			continue
		}
		result = append(result, entry)
	}
	pythonBin := filepath.Dir(filepath.Join(root, filepath.FromSlash(m.Python)))
	path := strings.Join([]string{pythonBin, filepath.Join(root, "packages", ScriptsDirectory), os.Getenv("PATH")}, string(os.PathListSeparator))
	return append(result, "PATH="+path, "PYTHONDONTWRITEBYTECODE=1", "PYTHONNOUSERSITE=1", "PYTHONPATH="+filepath.Join(root, "packages"))
}

// CacheDirectory keeps build-time materialization and deployed launches in the same shared store.
func CacheDirectory(override string, userCache func() (string, error)) (string, error) {
	if override == "" {
		base, err := userCache()
		if err != nil {
			return "", err
		}
		override = filepath.Join(base, "harnest", "agents")
	}
	return filepath.Abs(override)
}

// ScriptsDirectory is uv pip --target's layout on every host, including Windows.
const ScriptsDirectory = "bin"
