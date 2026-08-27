//go:build windows && arm64 && !harnest_release

package uvbootstrap

import _ "embed"

//go:embed assets/placeholder.txt
var embeddedUV []byte

const embeddedName = "uv.exe"
