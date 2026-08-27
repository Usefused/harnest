package uvbootstrap

import (
	"strings"
	"testing"
)

func TestPinnedBootstrapVersions(t *testing.T) {
	if Version != "0.12.6" {
		t.Fatalf("unexpected uv version %q", Version)
	}
	if ManagedPythonVersion != "3.12" {
		t.Fatalf("unexpected managed Python version %q", ManagedPythonVersion)
	}
}

func TestSourceBuildRejectsPlaceholderUV(t *testing.T) {
	_, err := Embedded()
	if err == nil || !strings.Contains(err.Error(), "no embedded uv") {
		t.Fatalf("got error %v, want source-build diagnostic", err)
	}
}
