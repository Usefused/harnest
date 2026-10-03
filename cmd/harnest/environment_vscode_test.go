package main

import (
	"bytes"
	"testing"
)

// TestVSCodeInterpreterUpdatesPreserveJSONC verifies targeted edits without rewriting editor preferences.
func TestVSCodeInterpreterUpdatesPreserveJSONC(t *testing.T) {
	cases := []struct{ name, source, want string }{
		{"comments", "{\n // choice\n \"python.defaultInterpreterPath\" /* keep */: \"old\", // note\n \"editor.tabSize\": 4,\n}", "{\n // choice\n \"python.defaultInterpreterPath\" /* keep */: \"new\", // note\n \"editor.tabSize\": 4,\n}"},
		{"nested key", `{"nested":{"python.defaultInterpreterPath":"keep"},"python.defaultInterpreterPath":"old"}`, `{"nested":{"python.defaultInterpreterPath":"keep"},"python.defaultInterpreterPath":"new"}`},
		{"duplicates", `{"python.defaultInterpreterPath":"one","python.defaultInterpreterPath":"two"}`, `{"python.defaultInterpreterPath":"new","python.defaultInterpreterPath":"new"}`},
		{"escaped key", `{"python.defaultInterpreter\u0050ath":"old"}`, `{"python.defaultInterpreter\u0050ath":"new"}`},
		{"wrong value type", `{"python.defaultInterpreterPath":null}`, `{"python.defaultInterpreterPath":"new"}`},
		{"unchanged", `{"python.defaultInterpreterPath":"new"}`, `{"python.defaultInterpreterPath":"new"}`},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			got, changed, err := setVSCodeInterpreter([]byte(test.source), "new")
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != test.want {
				t.Fatalf("got %s, want %s", got, test.want)
			}
			if changed != (test.source != test.want) {
				t.Fatalf("incorrect changed flag: %v", changed)
			}
			second, changed, err := setVSCodeInterpreter(got, "new")
			if err != nil || changed || !bytes.Equal(second, got) {
				t.Fatalf("second sync changed settings: %s: %v", second, err)
			}
		})
	}
}

// TestVSCodeInterpreterRejectsMalformedSettings prevents destructive recovery of editor files.
func TestVSCodeInterpreterRejectsMalformedSettings(t *testing.T) {
	for _, source := range []string{"[]", "null", "{invalid}", "{/* unfinished"} {
		if _, _, err := setVSCodeInterpreter([]byte(source), "new"); err == nil {
			t.Fatalf("accepted invalid settings: %s", source)
		}
	}
}
