package engine

import (
	"os"
	"path/filepath"
	"testing"
)

// TestLoadBundleServerSettings preserves inline templates and rejects unknown fields.
func TestLoadBundleServerSettings(t *testing.T) {
	project := t.TempDir()
	directory := writeAgent(t, project, "server-agent", true)
	path := filepath.Join(directory, "config.yaml")
	original, err := os.ReadFile(path)
	// Keep fixture I/O failures distinct from configuration validation failures.
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name, section string
		valid         bool
	}{
		{"partial", "server:\n  live: true\n  http:\n    port: ${PORT}\n  playground:\n    enabled: false\n", true},
		{"empty", "server: {}\n", true},
		{"unknown section", "server:\n  tls: true\n", false},
		{"unknown field", "server:\n  http:\n    prot: 9090\n", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// Each case starts from the same valid deployment contract.
			if err := os.WriteFile(path, append(append([]byte{}, original...), []byte(tc.section)...), 0o600); err != nil {
				t.Fatal(err)
			}
			bundle, err := LoadBundle(directory)
			// Shape errors must fail during Go loading, before the Python compiler runs.
			if !tc.valid {
				if err == nil {
					t.Fatal("expected strict server settings error")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			// Scalars remain templates; Python owns runtime resolution and range checks.
			if tc.name == "partial" && (bundle.Config.Server.HTTP.Port != "${PORT}" || bundle.Config.Server.Live != true) {
				t.Fatalf("lost server environment reference: %#v", bundle.Config.Server)
			}
		})
	}
}
