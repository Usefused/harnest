// Starter files that differ by agent framework, read from one folder per framework under internal/pysource/files/init/frameworks/.

package main

import (
	"fmt"
	"path"
	"strings"

	"harnest.dev/harnest/internal/frameworks"
	"harnest.dev/harnest/internal/pysource"
)

// Each framework in frameworks.json has a folder of starter files under
// internal/pysource/files/init/frameworks/<name>/, so init itself names no
// framework:
//
//   - advanced/ holds the files `harnest init --mode advanced` writes, at least
//     agent.py (or agent.py.tmpl).
//   - starter/ optionally replaces shared starter files for every mode.
//
// Templates receive {{.Name}}, the agent's Python identifier. Adding a framework
// means adding its folder; TestEveryFrameworkHasAScaffold checks the two match.
const frameworkScaffoldRoot = "init/frameworks/"

// frameworkScaffoldFiles renders one framework's starter/ or advanced/ files,
// keyed by the path they are written to in the new agent.
func frameworkScaffoldFiles(framework, kind, identifier string) map[string]string {
	if !frameworks.Known(framework) {
		panic(fmt.Sprintf("framework %q has no init scaffold", framework))
	}
	folder := frameworkScaffoldRoot + framework + "/" + kind + "/"
	files := map[string]string{}
	for _, name := range pysource.Names(folder) {
		target := strings.TrimSuffix(strings.TrimPrefix(name, folder), ".tmpl")
		files[path.Clean(target)] = pysource.Source(name, map[string]any{"Name": identifier})
	}
	return files
}

// applyFrameworkStarterFiles lets the selected framework replace shared starter files.
func applyFrameworkStarterFiles(files map[string]string, framework, identifier string) {
	for target, content := range frameworkScaffoldFiles(framework, "starter", identifier) {
		files[target] = content
	}
}
