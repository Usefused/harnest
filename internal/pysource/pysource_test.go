package pysource

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRenderFillsPlaceholders(t *testing.T) {
	source := Render("add/tool.py.tmpl", map[string]any{"Name": "lookup_order"})
	for _, want := range []string{"def lookup_order(request: str)", `"""Handle one lookup order request."""`} {
		if !strings.Contains(source, want) {
			t.Fatalf("rendered tool lacks %q:\n%s", want, source)
		}
	}
}

func TestRenderRequiresEveryPlaceholder(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("Render accepted a template with a missing field")
		}
	}()
	Render("add/tool.py.tmpl", map[string]any{})
}

func TestFileOnlyReturnsPlainSources(t *testing.T) {
	if !strings.Contains(File("init/tools/echo.py"), "def echo(message: str) -> str:") {
		t.Fatal("echo tool source changed unexpectedly")
	}
	for _, name := range []string{"add/tool.py.tmpl", "init/missing.py"} {
		func() {
			defer func() {
				if recover() == nil {
					t.Fatalf("File(%q) did not panic", name)
				}
			}()
			File(name)
		}()
	}
}

// Every file under files/ must be referenced by Go code or another template,
// so a renamed or removed caller cannot leave dead Python behind.
func TestEveryFileIsUsed(t *testing.T) {
	var callers strings.Builder
	for _, root := range []string{"../../cmd", "../../internal", "../../engine"} {
		err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
			if err != nil || entry.IsDir() || strings.HasSuffix(path, "_test.go") {
				return err
			}
			if strings.HasSuffix(path, ".go") || strings.HasSuffix(path, templateSuffix) {
				data, err := os.ReadFile(path)
				callers.Write(data)
				return err
			}
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	for name := range sources {
		if !strings.Contains(callers.String(), `"`+name+`"`) {
			t.Errorf("files/%s is not used by any Go code or template", name)
		}
	}
}
