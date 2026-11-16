//go:build linux && amd64 && harnest_release

// Embeds the prebuilt agent launcher for Linux amd64 release builds.

package agentlauncher

import _ "embed"

//go:embed assets/agent_linux_amd64
var embeddedLauncher []byte
