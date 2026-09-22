//go:build linux && arm64 && harnest_release

package agentlauncher

import _ "embed"

//go:embed assets/agent_linux_arm64
var embeddedLauncher []byte
