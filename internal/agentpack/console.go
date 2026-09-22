package agentpack

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
)

const ConsoleScriptSuffix = ".harnest.py"

const consoleBootstrap = `import site,sys; site.addsitedir(sys.argv.pop(1)); script=sys.argv.pop(1); sys.argv=sys.argv[1:]; __file__=script; exec(compile(open(script,"rb").read(),script,"exec"))`

// ConsoleCommand recognizes shared Windows tool launchers and checks the entire
// manifest before running their sidecar with the pack's isolated interpreter.
func ConsoleCommand(filename string, args []string) (*exec.Cmd, error) {
	if runtime.GOOS != "windows" {
		return nil, nil
	}
	if _, err := os.Lstat(filename + ConsoleScriptSuffix); os.IsNotExist(err) {
		return nil, nil
	}
	root := filepath.Dir(filepath.Dir(filepath.Dir(filename)))
	if filepath.Dir(filename) != filepath.Join(root, "packages", ScriptsDirectory) {
		return nil, fmt.Errorf("console launcher must remain inside its runtime pack")
	}
	m, err := ReadManifest(root)
	if err != nil {
		return nil, err
	}
	if err = m.CheckHost(); err != nil {
		return nil, err
	}
	if err = verifyTree(root, m); err != nil {
		return nil, err
	}
	argv := []string{"-I", "-B", "-c", consoleBootstrap, filepath.Join(root, "packages"), filename + ConsoleScriptSuffix, filename}
	command := exec.Command(filepath.Join(root, filepath.FromSlash(m.Python)), append(argv, args...)...)
	command.Env = Environment(root, m)
	return command, nil
}
