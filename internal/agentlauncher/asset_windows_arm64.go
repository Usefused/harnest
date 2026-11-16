//go:build windows && arm64 && harnest_release

// Embeds the prebuilt agent launcher for Windows arm64 release builds.

package agentlauncher

import _ "embed"

//go:embed assets/agent_windows_arm64
var embeddedLauncher []byte
