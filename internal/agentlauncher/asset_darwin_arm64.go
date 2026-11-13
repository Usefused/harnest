//go:build darwin && arm64 && harnest_release

// Embeds the prebuilt agent launcher for macOS arm64 release builds.

package agentlauncher

import _ "embed"

//go:embed assets/agent_darwin_arm64
var embeddedLauncher []byte
