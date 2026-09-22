//go:build linux && amd64 && harnest_release

package agentlauncher

import _ "embed"

//go:embed assets/agent_linux_amd64
var embeddedLauncher []byte
