// Package agentlauncher supplies a small native bootstrap without the CLI's build assets.
package agentlauncher

import "fmt"

// Embedded returns the release-matched native executable for this host.
func Embedded() ([]byte, error) {
	if len(embeddedLauncher) == 0 {
		return nil, fmt.Errorf("agent launcher missing; use a release build or build from the source checkout")
	}
	return embeddedLauncher, nil
}
