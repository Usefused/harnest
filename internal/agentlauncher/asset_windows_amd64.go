//go:build windows && amd64 && harnest_release

// Embeds the prebuilt agent launcher for Windows amd64 release builds.

package agentlauncher

import _ "embed"

//go:embed assets/agent_windows_amd64
var embeddedLauncher []byte
