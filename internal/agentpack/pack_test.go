package agentpack

import (
	"archive/zip"
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
)

// fixturePython uses native executable naming without needing an installed Python.
func fixturePython() string {
	if runtime.GOOS == "windows" {
		return "python/python.exe"
	}
	return "python/bin/python3"
}

// fixturePack creates two paths sharing one object, plus an executable interpreter stand-in.
func fixturePack(t *testing.T, root, store string) Manifest {
	t.Helper()
	if err := SupportedHost(); err != nil {
		t.Skip(err)
	}
	source := filepath.Join(t.TempDir(), "source")
	writeFixture(t, source, fixturePython(), []byte("#!/bin/sh\nexit 0\n"), 0755)
	writeFixture(t, source, "packages/one.py", []byte("shared dependency"), 0644)
	writeFixture(t, source, "packages/two.py", []byte("shared dependency"), 0644)
	files, err := ScanTree(source, "", store)
	if err != nil {
		t.Fatal(err)
	}
	m := Manifest{Format: Format, OS: runtime.GOOS, Arch: runtime.GOARCH, Python: fixturePython(), Version: "3.12.1", Harnest: "1.0", Inputs: []string{"agent"}, Files: files}
	m.Seal()
	for _, f := range files {
		if err := LinkObject(filepath.Join(store, f.Object), filepath.Join(root, "objects", f.Object), f); err != nil {
			t.Fatal(err)
		}
	}
	if err := WriteManifest(root, m); err != nil {
		t.Fatal(err)
	}
	return m
}

// writeFixture retains authored executable bits so permission-sensitive identities are tested.
func writeFixture(t *testing.T, root, name string, data []byte, mode os.FileMode) {
	t.Helper()
	target := filepath.Join(root, name)
	if err := os.MkdirAll(filepath.Dir(target), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, data, mode); err != nil {
		t.Fatal(err)
	}
}

// assertShared verifies physical disk deduplication rather than equal file contents.
func assertShared(t *testing.T, left, right string) {
	t.Helper()
	a, err := os.Stat(left)
	if err != nil {
		t.Fatal(err)
	}
	b, err := os.Stat(right)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(a, b) {
		t.Fatalf("expected hardlinked dependency files: %s and %s", left, right)
	}
}

// TestPacksAndRuntimeTreesDeduplicate proves sharing across packs, versions, and launch caches.
func TestPacksAndRuntimeTreesDeduplicate(t *testing.T) {
	root := t.TempDir()
	store := filepath.Join(root, "store")
	first := filepath.Join(root, "one")
	second := filepath.Join(root, "two")
	m := fixturePack(t, first, store)
	n := fixturePack(t, second, store)
	n.Inputs = []string{"different agent"}
	n.Seal()
	if err := WriteManifest(second, n); err != nil {
		t.Fatal(err)
	}
	object := m.Files[0].Object
	assertShared(t, filepath.Join(first, "objects", object), filepath.Join(second, "objects", object))
	entries, err := os.ReadDir(store)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 {
		t.Fatalf("want 2 unique objects, got %d", len(entries))
	}
	cache := filepath.Join(root, "cache")
	a, err := Materialize(first, cache, m)
	if err != nil {
		t.Fatal(err)
	}
	b, err := Materialize(second, cache, n)
	if err != nil {
		t.Fatal(err)
	}
	assertShared(t, filepath.Join(a, "packages/one.py"), filepath.Join(a, "packages/two.py"))
	assertShared(t, filepath.Join(a, "packages/one.py"), filepath.Join(b, "packages/one.py"))
	// Packs remain independently portable even when the build store is removed.
	if err := os.RemoveAll(store); err != nil {
		t.Fatal(err)
	}
	if _, err := Materialize(first, filepath.Join(root, "fresh-cache"), m); err != nil {
		t.Fatal(err)
	}
}

// TestMaterializeConcurrent makes simultaneous launches converge on one verified tree.
func TestMaterializeConcurrent(t *testing.T) {
	root := t.TempDir()
	pack := filepath.Join(root, "pack")
	m := fixturePack(t, pack, filepath.Join(root, "store"))
	var workers sync.WaitGroup
	for i := 0; i < 8; i++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			if _, err := Materialize(pack, filepath.Join(root, "cache"), m); err != nil {
				t.Error(err)
			}
		}()
	}
	workers.Wait()
}

// TestObjectCorruptionAndPermissionIdentity catches both damaged bytes and executable-bit aliasing.
func TestObjectCorruptionAndPermissionIdentity(t *testing.T) {
	root := t.TempDir()
	pack := filepath.Join(root, "pack")
	m := fixturePack(t, pack, filepath.Join(root, "store"))
	f := m.Files[0]
	object := filepath.Join(pack, "objects", f.Object)
	if err := os.Chmod(object, 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(object, bytes.Repeat([]byte("x"), int(f.Size)), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(object, 0444); err != nil {
		t.Fatal(err)
	}
	if _, err := Materialize(pack, filepath.Join(root, "cache"), m); err == nil {
		t.Fatal("corrupt object accepted")
	}
}

// TestObjectPermissionIdentity keeps native executables distinct from plain data.
func TestObjectPermissionIdentity(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "same-bytes")
	if err := os.WriteFile(source, []byte("same"), 0644); err != nil {
		t.Fatal(err)
	}
	regular, err := StoreFile(source, filepath.Join(root, "modes"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(source, 0755); err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS == "windows" {
		if err := os.Rename(source, source+".exe"); err != nil {
			t.Fatal(err)
		}
		source += ".exe"
	}
	executable, err := StoreFile(source, filepath.Join(root, "modes"))
	if err != nil {
		t.Fatal(err)
	}
	if regular.Object == executable.Object {
		t.Fatal("executable and data file share mutable permissions")
	}
}

// TestManifestRejectsUnsafeTrees validates paths before creating directories or links.
func TestManifestRejectsUnsafeTrees(t *testing.T) {
	root := t.TempDir()
	m := fixturePack(t, filepath.Join(root, "pack"), filepath.Join(root, "store"))
	for _, bad := range []File{{Path: "../outside", Object: m.Files[0].Object}, {Path: "packages", Link: "../../outside"}, {Path: "packages", Link: "python"}, {Path: fixturePython(), Link: "/outside"}} {
		t.Run(bad.Path+bad.Link, func(t *testing.T) {
			n := m
			n.Files = append(append([]File{}, m.Files...), bad)
			n.Seal()
			if err := n.Validate(); err == nil {
				t.Fatal("unsafe runtime manifest accepted")
			}
		})
	}
	m.OS = "other"
	m.Seal()
	if _, err := Materialize(filepath.Join(root, "pack"), filepath.Join(root, "cache"), m); err == nil {
		t.Fatal("incompatible platform accepted")
	}
}

// TestExecutableAttachmentDoesNotCopyDependencies distinguishes references from opt-in embedding.
func TestExecutableAttachmentDoesNotCopyDependencies(t *testing.T) {
	root := t.TempDir()
	pack := filepath.Join(root, "pack")
	m := fixturePack(t, pack, filepath.Join(root, "store"))
	artifact := filepath.Join(root, "artifact")
	writeFixture(t, artifact, "agent.py", []byte("agent"), 0644)
	for _, embedded := range []bool{false, true} {
		output := filepath.Join(root, "executable")
		if err := WriteExecutable(output, []byte("native launcher"), artifact, pack, m, embedded, nil); err != nil {
			t.Fatal(err)
		}
		archive, metadata, err := ReadExecutable(output)
		if err != nil {
			t.Fatal(err)
		}
		found := false
		for _, f := range archive.File {
			found = found || strings.HasPrefix(f.Name, "runtime/objects/")
		}
		archive.Close()
		if found != embedded || metadata.Runtime.Digest != m.Digest {
			t.Fatal("runtime attachment policy changed")
		}
	}
}

// TestExtractionRejectsTraversal ensures appended archives cannot write outside their staging root.
func TestExtractionRejectsTraversal(t *testing.T) {
	var buffer bytes.Buffer
	writer := zip.NewWriter(&buffer)
	file, err := writer.Create("agent/../escape")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = file.Write([]byte("bad")); err != nil {
		t.Fatal(err)
	}
	if err = writer.Close(); err != nil {
		t.Fatal(err)
	}
	reader, err := zip.NewReader(bytes.NewReader(buffer.Bytes()), int64(buffer.Len()))
	if err != nil {
		t.Fatal(err)
	}
	if err = ExtractPrefix(&Archive{Reader: reader}, "agent", t.TempDir()); err == nil {
		t.Fatal("traversal accepted")
	}
}

// TestCachedRuntimeRejectsInjectedModules protects the cache reuse path, not only initial extraction.
func TestCachedRuntimeRejectsInjectedModules(t *testing.T) {
	root := t.TempDir()
	pack := filepath.Join(root, "pack")
	cache := filepath.Join(root, "cache")
	m := fixturePack(t, pack, filepath.Join(root, "store"))
	tree, err := Materialize(pack, cache, m)
	if err != nil {
		t.Fatal(err)
	}
	writeFixture(t, tree, "packages/injected.py", []byte("untrusted"), 0600)
	if _, err := Materialize(pack, cache, m); err == nil || !strings.Contains(err.Error(), "unmanifested") {
		t.Fatalf("injected module accepted: %v", err)
	}
}

// TestVerifiedCopyFallbackPublishesCompleteBytes exercises the no-hardlink path independently of the host filesystem.
func TestVerifiedCopyFallbackPublishesCompleteBytes(t *testing.T) {
	root := t.TempDir()
	m := fixturePack(t, filepath.Join(root, "pack"), filepath.Join(root, "store"))
	f := m.Files[0]
	source := filepath.Join(root, "store", f.Object)
	destination := filepath.Join(root, "copied")
	if err := copyExclusive(source, destination, f); err != nil {
		t.Fatal(err)
	}
	if err := VerifyObject(destination, f); err != nil {
		t.Fatal(err)
	}
	if err := copyExclusive(source, destination, f); err != nil {
		t.Fatal(err)
	}
}
