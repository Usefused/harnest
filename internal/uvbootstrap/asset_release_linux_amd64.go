//go:build linux && amd64 && harnest_release

package uvbootstrap

import _ "embed"

//go:embed assets/release/uv_linux_amd64
var embeddedUV []byte

const embeddedName = "uv"
