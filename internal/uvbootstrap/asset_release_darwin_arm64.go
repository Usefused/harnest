//go:build darwin && arm64 && harnest_release

package uvbootstrap

import _ "embed"

//go:embed assets/release/uv_darwin_arm64
var embeddedUV []byte

const embeddedName = "uv"
