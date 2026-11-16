//go:build linux && arm64 && harnest_release

// Embeds the prebuilt agent launcher for Linux arm64 release builds.

package agentlauncher

import _ "embed"

//go:embed assets/agent_linux_arm64
var embeddedLauncher []byte
