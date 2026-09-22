package agentpack

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// TestConsoleBootstrapPreservesMainAndArguments exercises real Python semantics,
// including package discovery, __main__ identity, spaces and Unicode arguments.
func TestConsoleBootstrapPreservesMainAndArguments(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		python, err = exec.LookPath("python")
	}
	if err != nil {
		t.Skip("console bootstrap integration requires Python")
	}
	root := t.TempDir()
	script := filepath.Join(root, "tool.exe.harnest.py")
	body := `import __main__, helper, sys
assert __main__.__file__ == __file__
assert helper.value == 42
assert sys.argv == ["tool.exe", "space value", "Ω"]
print("portable-console")
`
	if err := os.WriteFile(script, []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "helper.py"), []byte("value=42\n"), 0600); err != nil {
		t.Fatal(err)
	}
	output, err := exec.Command(python, "-I", "-B", "-c", consoleBootstrap, root, script, "tool.exe", "space value", "Ω").CombinedOutput()
	if err != nil || strings.TrimSpace(string(output)) != "portable-console" {
		t.Fatalf("console bootstrap: %v: %s", err, output)
	}
}
