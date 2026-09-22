package agentpack

import "testing"

// TestWindowsPathAliasesAreRejected exercises Win32 names even on Unix CI hosts.
func TestWindowsPathAliasesAreRejected(t *testing.T) {
	for _, name := range []string{"AUX.py", "packages/CON", "COM1.txt", "LPT²", "NUL.", "trailing ", "stream:alternate", "wild*card", "newline\n.py", "CONIN$", "CON .py"} {
		if safeWindowsPath(name) {
			t.Errorf("accepted unsafe Windows path %q", name)
		}
	}
	for _, name := range []string{"python/python.exe", "packages/COM10.py", "packages/compile.py", "spaces allowed/file.py"} {
		if !safeWindowsPath(name) {
			t.Errorf("rejected safe Windows path %q", name)
		}
	}
	for _, files := range [][]File{
		{{Path: "/"}},
		{{Path: "/packages/tool.py"}},
		{{Path: "packages/A.py"}, {Path: "packages/a.py"}},
		{{Path: "packages/A"}, {Path: "packages/a/tool.py"}},
		{{Path: "Packages/a.py"}, {Path: "packages/b.py"}},
		{{Path: "python/python.exe", Link: "alias.exe"}},
	} {
		if validateWindowsTree(files) == nil {
			t.Fatalf("accepted aliased Windows tree: %#v", files)
		}
	}
}
