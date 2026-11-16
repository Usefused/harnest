//go:build windows && amd64 && !harnest_release

// Embeds a placeholder in place of uv for development builds on Windows amd64.

package uvbootstrap

import _ "embed"

//go:embed assets/placeholder.txt
var embeddedUV []byte

const embeddedName = "uv.exe"
