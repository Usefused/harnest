//go:build windows && arm64 && harnest_release

package agentlauncher

import _ "embed"

//go:embed assets/agent_windows_arm64
var embeddedLauncher []byte
