// Package uvbootstrap owns the native uv executable embedded in release CLIs.
package uvbootstrap

import (
	"bytes"
	_ "embed"
	"fmt"
	"strings"
)

const (
	ManagedPythonVersion = "3.12"
	placeholderPrefix    = "harnest uv placeholder"
)

//go:embed version.txt
var versionText string

// Version is the uv release pinned into Harnest release binaries.
var Version = strings.TrimSpace(versionText)

// Artifact is the platform-native uv executable carried by the Go CLI.
type Artifact struct {
	Name     string
	Contents []byte
}

// Embedded returns the selected platform asset and rejects source placeholders.
func Embedded() (Artifact, error) {
	if len(embeddedUV) == 0 || bytes.HasPrefix(embeddedUV, []byte(placeholderPrefix)) {
		return Artifact{}, fmt.Errorf("release binary has no embedded uv %s executable", Version)
	}
	return Artifact{Name: embeddedName, Contents: embeddedUV}, nil
}
