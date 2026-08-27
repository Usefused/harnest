//go:build darwin && amd64 && harnest_release

package uvbootstrap

import _ "embed"

//go:embed assets/release/uv_darwin_amd64
var embeddedUV []byte

const embeddedName = "uv"
