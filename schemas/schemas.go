// Package schemas gives Go code the same published JSON schemas users validate
// their files against, so the CLI and engine never keep their own copies.
package schemas

import (
	_ "embed"
	"encoding/json"
	"fmt"
)

//go:embed extension.schema.json
var extensionSchema []byte

// extensionCapabilities is the closed capability vocabulary of extension.yaml.
// The Python compiler's EXTENSION_CAPABILITIES is checked against the same enum
// by test_manifest_schema_uses_runtime_capability_vocabulary.
var extensionCapabilities = mustEnum(extensionSchema)

// KnownExtensionCapability reports whether an extension manifest may declare name.
func KnownExtensionCapability(name string) bool {
	_, known := extensionCapabilities[name]
	return known
}

// mustEnum reads properties.capabilities.items.enum; a malformed embedded schema
// is a build defect, so it fails at startup rather than accepting everything.
func mustEnum(document []byte) map[string]struct{} {
	var schema struct {
		Properties struct {
			Capabilities struct {
				Items struct {
					Enum []string `json:"enum"`
				} `json:"items"`
			} `json:"capabilities"`
		} `json:"properties"`
	}
	if err := json.Unmarshal(document, &schema); err != nil {
		panic(fmt.Sprintf("schemas: decode extension.schema.json: %v", err))
	}
	values := schema.Properties.Capabilities.Items.Enum
	if len(values) == 0 {
		panic("schemas: extension.schema.json declares no capabilities")
	}
	set := make(map[string]struct{}, len(values))
	for _, value := range values {
		set[value] = struct{}{}
	}
	return set
}
