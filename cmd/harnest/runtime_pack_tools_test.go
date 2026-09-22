package main

import (
	"archive/zip"
	"os"
	"path/filepath"
	"testing"

	"harnest.dev/harnest/internal/agentpack"
)

// TestWindowsConsoleRelocationSharesLaunchers models an installer's native prefix
// plus Python ZIP entrypoint; changing build paths cannot change shared objects.
func TestWindowsConsoleRelocationSharesLaunchers(t *testing.T) {
	root := t.TempDir()
	launcher := []byte("identical native launcher")
	for _, name := range []string{"first.exe", "second.exe"} {
		filename := filepath.Join(root, name)
		writeWindowsToolFixture(t, filename, name)
		if err := relocateWindowsTool(filename, launcher); err != nil {
			t.Fatal(err)
		}
		if string(mustReadTestFile(t, filename)) != string(launcher) {
			t.Fatal("entrypoint launcher was not replaced")
		}
		if string(mustReadTestFile(t, filename+agentpack.ConsoleScriptSuffix)) != "print('portable')\n" {
			t.Fatal("entrypoint retained build path")
		}
	}
	first, err := agentpack.StoreFile(filepath.Join(root, "first.exe"), filepath.Join(root, "store"))
	if err != nil {
		t.Fatal(err)
	}
	second, err := agentpack.StoreFile(filepath.Join(root, "second.exe"), filepath.Join(root, "store"))
	if err != nil {
		t.Fatal(err)
	}
	if first.Object != second.Object {
		t.Fatal("console launchers cannot deduplicate")
	}
}

// TestWindowsNativeToolsArePreserved prevents rewriting wheel-supplied native binaries.
func TestWindowsNativeToolsArePreserved(t *testing.T) {
	filename := filepath.Join(t.TempDir(), "native.exe")
	mustWriteEnvironmentFixture(t, filename, "MZ-native-tool")
	if err := relocateWindowsTool(filename, []byte("replacement")); err != nil {
		t.Fatal(err)
	}
	if string(mustReadTestFile(t, filename)) != "MZ-native-tool" {
		t.Fatal("native tool rewritten")
	}
}

// writeWindowsToolFixture represents the public zipapp layout used by Python installers.
func writeWindowsToolFixture(t *testing.T, filename, name string) {
	t.Helper()
	file, err := os.Create(filename)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = file.Write([]byte("MZ-native-prefix")); err != nil {
		t.Fatal(err)
	}
	archive := zip.NewWriter(file)
	script, err := archive.Create("__main__.py")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = script.Write([]byte("#!C:\\build\\" + name + "\\python.exe\nprint('portable')\n")); err != nil {
		t.Fatal(err)
	}
	if err = archive.Close(); err != nil {
		t.Fatal(err)
	}
	// Windows PE resources may pad the ZIP rather than ending exactly at its footer.
	if _, err = file.Write(make([]byte, 512)); err != nil {
		t.Fatal(err)
	}
	if err = file.Close(); err != nil {
		t.Fatal(err)
	}
}
