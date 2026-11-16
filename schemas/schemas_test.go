package schemas

import "testing"

func TestExtensionCapabilitiesComeFromTheSchema(t *testing.T) {
	for _, name := range []string{"lifecycle.tool", "framework.extension", "storage.memory", "sandbox.provider"} {
		if !KnownExtensionCapability(name) {
			t.Fatalf("%s should be a known extension capability", name)
		}
	}
	for _, name := range []string{"", "native.adk", "lifecycle.unknown"} {
		if KnownExtensionCapability(name) {
			t.Fatalf("%q should not be a known extension capability", name)
		}
	}
}
