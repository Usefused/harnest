//go:build windows && amd64 && harnest_release

package agentlauncher

import _ "embed"

//go:embed assets/agent_windows_amd64
var embeddedLauncher []byte
