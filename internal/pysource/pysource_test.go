package pysource

import (
	"io/fs"
	"os"
	"path"
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
// by its own name or a folder read with Names, so a renamed or removed caller
// cannot leave dead Python behind.
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
		if !referenced(callers.String(), name) {
			t.Errorf("files/%s is not used by any Go code or template", name)
		}
	}
}

// referenced reports whether code quotes name or one of its parent folders.
func referenced(code, name string) bool {
	if strings.Contains(code, `"`+name+`"`) {
		return true
	}
	for folder := path.Dir(name); folder != "."; folder = path.Dir(folder) {
		if strings.Contains(code, `"`+folder+`/"`) {
			return true
		}
	}
	return false
}

func TestNamesAndSourceReadAFolder(t *testing.T) {
	names := Names("init/frameworks/langgraph/advanced/")
	if len(names) != 3 || names[0] != "init/frameworks/langgraph/advanced/agent.py.tmpl" {
		t.Fatalf("unexpected LangGraph advanced files: %v", names)
	}
	if !strings.Contains(Source(names[0], map[string]any{"Name": "demo"}), `name="demo"`) {
		t.Fatal("Source did not render the template")
	}
	if Source(names[1], nil) != File(names[1]) {
		t.Fatal("Source changed a plain file")
	}
}

func TestLocalClutterIsNeverEmbedded(t *testing.T) {
	for _, item := range []struct {
		name string
		dir  bool
		want bool
	}{
		{"files/init/tools/echo.py", false, false},
		{"files/init/subagents/__init__.py", false, false},
		{"files/init/tools/__pycache__", true, true},
		{"files/init/tools/echo.cpython-313.pyc", false, true},
		{"files/init/.DS_Store", false, true},
		{"files/.git", true, true},
		{"files", true, false},
	} {
		if got := ignored(item.name, item.dir); got != item.want {
			t.Errorf("ignored(%q, %v) = %v, want %v", item.name, item.dir, got, item.want)
		}
	}
}
