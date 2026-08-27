//go:build linux && arm64 && harnest_release

package uvbootstrap

import _ "embed"

//go:embed assets/release/uv_linux_arm64
var embeddedUV []byte

const embeddedName = "uv"
