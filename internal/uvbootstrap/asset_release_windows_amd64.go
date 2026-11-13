//go:build windows && amd64 && harnest_release

// Embeds the uv binary for release builds on Windows amd64.

package uvbootstrap

import _ "embed"

//go:embed assets/release/uv_windows_amd64.exe
var embeddedUV []byte

const embeddedName = "uv.exe"
