// Package playground embeds development UI assets in the native CLI, not the Python wheel.
package playground

import "embed"

// Files contains only browser assets; compiled agent artifacts never copy it.
//
//go:embed *.html *.css *.js *.txt
var Files embed.FS
