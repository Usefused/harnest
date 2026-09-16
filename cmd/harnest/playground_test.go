package main

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// withoutPlaygroundArgument verifies the transient CLI handoff before comparing HTTP flags.
func withoutPlaygroundArgument(t *testing.T, call string) string {
	t.Helper()
	args := strings.Split(call, "\t")
	index := slices.Index(args, "--playground-assets")
	if index < 0 || index+1 >= len(args) {
		t.Fatalf("missing playground argument: %s", call)
	}
	if _, err := os.Stat(args[index+1]); !os.IsNotExist(err) {
		t.Fatalf("serve did not release playground assets: %v", err)
	}
	return strings.Join(append(args[:index], args[index+2:]...), "\t")
}

func TestPlaygroundAssetsAreCLIOwnedAndEphemeral(t *testing.T) {
	directory, cleanup, err := stagePlayground()
	if err != nil {
		t.Fatal(err)
	}
	defer cleanup()
	for _, name := range []string{"index.html", "playground.css", "playground.js", "selects.js", "selects.css", "markdown.js", "markdown-it.min.js"} {
		if _, err := os.Stat(filepath.Join(directory, name)); err != nil {
			t.Fatal(err)
		}
	}
	args := (serveOptions{playgroundAssets: directory}).arguments("agent")
	if index := slices.Index(args, "--playground-assets"); index < 0 || args[index+1] != directory {
		t.Fatalf("missing CLI asset handoff: %v", args)
	}
	cleanup()
	if _, err := os.Stat(directory); !os.IsNotExist(err) {
		t.Fatalf("playground assets survived CLI shutdown: %v", err)
	}
}

// TestAuthoringWorkspaceIsExplicit verifies the separate source handoff used by reload.
func TestAuthoringWorkspaceIsExplicit(t *testing.T) {
	ordinary := (serveOptions{playgroundAssets: "assets"}).arguments("agent")
	if slices.Contains(ordinary, "--authoring-root") {
		t.Fatal("ordinary launch gained source write authority")
	}
	development := (serveOptions{reload: true, authoringRoot: "/tmp/source", playgroundAssets: "assets"}).arguments("agent")
	index := slices.Index(development, "--authoring-root")
	if index < 0 || development[index+1] != "/tmp/source" {
		t.Fatalf("missing authoring handoff: %v", development)
	}
}
