//go:build darwin && amd64 && harnest_release

// Embeds the uv binary for release builds on macOS amd64.

package uvbootstrap

import _ "embed"

//go:embed assets/release/uv_darwin_amd64
var embeddedUV []byte

const embeddedName = "uv"
