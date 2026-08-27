package runtimewheel

import (
	"archive/zip"
	"bytes"
	"strings"
	"testing"
	"testing/fstest"
)

func TestArtifactFromFSRequiresExactlyOneWheel(t *testing.T) {
	tests := []struct {
		name  string
		files fstest.MapFS
		want  string
	}{
		{name: "missing", files: fstest.MapFS{"assets/README.txt": {}}, want: "found 0"},
		{name: "multiple", files: fstest.MapFS{
			"assets/one.whl": {}, "assets/two.whl": {},
		}, want: "found 2"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := artifactFromFS(test.files)
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("got error %v, want %q", err, test.want)
			}
		})
	}
}

func TestMetadataVersionReadsWheelDistributionMetadata(t *testing.T) {
	version, err := metadataVersion(testWheel(t, "0.1.2"))
	if err != nil {
		t.Fatal(err)
	}
	if version != "0.1.2" {
		t.Fatalf("got version %q", version)
	}
}

func TestArtifactForVersionRejectsCLIMismatch(t *testing.T) {
	files := fstest.MapFS{
		"assets/harnest-0.1.2-py3-none-any.whl": {
			Data: testWheel(t, "0.1.2"),
		},
	}
	artifact, err := artifactForVersion(files, "v0.1.2")
	if err != nil || artifact.Name != "harnest-0.1.2-py3-none-any.whl" {
		t.Fatalf("matching artifact = %#v, %v", artifact, err)
	}
	_, err = artifactForVersion(files, "0.1.3")
	if err == nil || !strings.Contains(err.Error(), "does not match CLI version") {
		t.Fatalf("got mismatch error %v", err)
	}
}

func TestEmbeddedSourceBuildHasNoReleaseWheel(t *testing.T) {
	_, err := Embedded("dev")
	if err == nil || !strings.Contains(err.Error(), "found 0") {
		t.Fatalf("got error %v, want source-build diagnostic", err)
	}
}

func testWheel(t *testing.T, version string) []byte {
	t.Helper()
	var contents bytes.Buffer
	writer := zip.NewWriter(&contents)
	metadata, err := writer.Create("harnest-" + version + ".dist-info/METADATA")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := metadata.Write([]byte("Metadata-Version: 2.4\nName: harnest\nVersion: " + version + "\n")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return contents.Bytes()
}
