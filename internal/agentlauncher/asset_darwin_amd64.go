//go:build darwin && amd64 && harnest_release

package agentlauncher

import _ "embed"

//go:embed assets/agent_darwin_amd64
var embeddedLauncher []byte
