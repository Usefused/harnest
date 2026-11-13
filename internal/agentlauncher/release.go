//go:build harnest_release

// Returns the prebuilt, embedded launcher in release builds.

package agentlauncher

import "context"

// Load uses the prebuilt launcher so installed CLIs never need a Go toolchain.
func Load(_ context.Context) ([]byte, error) { return Embedded() }
