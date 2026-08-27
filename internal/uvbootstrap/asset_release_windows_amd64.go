//go:build windows && amd64 && harnest_release

package uvbootstrap

import _ "embed"

//go:embed assets/release/uv_windows_amd64.exe
var embeddedUV []byte

const embeddedName = "uv.exe"
