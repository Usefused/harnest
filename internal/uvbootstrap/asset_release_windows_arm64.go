//go:build windows && arm64 && harnest_release

// Embeds the uv binary for release builds on Windows arm64.

package uvbootstrap

import _ "embed"

//go:embed assets/release/uv_windows_arm64.exe
var embeddedUV []byte

const embeddedName = "uv.exe"
