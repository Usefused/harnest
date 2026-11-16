//go:build darwin && amd64 && harnest_release

// Embeds the prebuilt agent launcher for macOS amd64 release builds.

package agentlauncher

import _ "embed"

//go:embed assets/agent_darwin_amd64
var embeddedLauncher []byte
