//go:build linux && arm64 && !harnest_release

// Embeds a placeholder in place of uv for development builds on Linux arm64.

package uvbootstrap

import _ "embed"

//go:embed assets/placeholder.txt
var embeddedUV []byte

const embeddedName = "uv"
