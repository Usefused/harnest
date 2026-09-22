//go:build darwin && arm64 && harnest_release

package agentlauncher

import _ "embed"

//go:embed assets/agent_darwin_arm64
var embeddedLauncher []byte
