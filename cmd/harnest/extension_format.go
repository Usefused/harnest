package main

import "strings"

// isExtensionProject accepts canonical names and the retained legacy namespace.
func isExtensionProject(name string) bool {
	normalized := normalizeProjectName(name)
	return strings.HasPrefix(normalized, pypiExtensionPrefix) || strings.HasPrefix(normalized, pypiPluginPrefix)
}

// extensionProjectSlug binds both distribution spellings to their package identity.
func extensionProjectSlug(name string) string {
	return strings.TrimPrefix(strings.TrimPrefix(normalizeProjectName(name), pypiExtensionPrefix), pypiPluginPrefix)
}

// extensionWheelFormat derives filenames only from a validated public entrypoint suffix.
func extensionWheelFormat(value string) (string, string) {
	if strings.HasSuffix(value, ".extension:extension") {
		return "extension", "Extension"
	}
	return "plugin", "RuntimePlugin"
}
