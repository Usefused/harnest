package main

import "strings"

// isExtensionProject accepts only the canonical public distribution namespace.
func isExtensionProject(name string) bool {
	normalized := normalizeProjectName(name)
	return strings.HasPrefix(normalized, pypiExtensionPrefix)
}

// extensionProjectSlug removes the canonical distribution prefix.
func extensionProjectSlug(name string) string {
	return strings.TrimPrefix(normalizeProjectName(name), pypiExtensionPrefix)
}

// extensionWheelFormat derives filenames only from a validated public entrypoint suffix.
func extensionWheelFormat(value string) (string, string) {
	if strings.HasSuffix(value, ".extension:extension") {
		return "extension", "Extension"
	}
	return "", ""
}
