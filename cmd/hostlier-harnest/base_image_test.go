package main

import (
	"context"
	"io"
	"log"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/registry"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/empty"
	"github.com/google/go-containerregistry/pkg/v1/mutate"
	"github.com/google/go-containerregistry/pkg/v1/remote"
)

// imagePins publishes real manifests and config blobs to a disposable OCI registry.
func imagePins(t *testing.T, platforms ...v1.Platform) (string, map[string]string) {
	t.Helper()
	server := httptest.NewServer(registry.New(registry.Logger(log.New(io.Discard, "", 0))))
	t.Cleanup(server.Close)
	repo := strings.TrimPrefix(server.URL, "http://") + "/base"
	var index v1.ImageIndex = empty.Index
	pins := map[string]string{}
	for _, platform := range platforms {
		img, err := mutate.ConfigFile(empty.Image, &v1.ConfigFile{OS: platform.OS, Architecture: platform.Architecture})
		if err != nil {
			t.Fatal(err)
		}
		digest, err := img.Digest()
		if err != nil {
			t.Fatal(err)
		}
		pins[platform.String()] = repo + "@" + digest.String()
		index = mutate.AppendManifests(index, mutate.IndexAddendum{Add: img, Descriptor: v1.Descriptor{Platform: &platform}})
	}
	tag, err := name.NewTag(repo + ":index")
	if err != nil {
		t.Fatal(err)
	}
	if err = remote.WriteIndex(tag, index); err != nil {
		t.Fatal(err)
	}
	digest, err := index.Digest()
	if err != nil {
		t.Fatal(err)
	}
	return repo + "@" + digest.String(), pins
}

// TestBaseImagePlatforms exercises both digest forms, including a real single-platform mismatch.
func TestBaseImagePlatforms(t *testing.T) {
	platforms := []v1.Platform{{OS: "linux", Architecture: "amd64"}, {OS: "linux", Architecture: "arm64"}, {OS: "windows", Architecture: "arm64"}}
	index, pins := imagePins(t, platforms...)
	for _, p := range platforms[:2] {
		for _, pin := range []string{index, pins[p.String()]} {
			settings := projectSettings{BaseImage: pin}
			got, err := settings.resolveBaseImage(context.Background(), p.Architecture)
			if err != nil || got != pins[p.String()] {
				t.Fatalf("%s: got %q, %v", p, got, err)
			}
		}
	}
	for _, wrong := range []string{"linux/amd64", "windows/arm64"} {
		settings := projectSettings{BaseImage: pins[wrong]}
		_, err := settings.resolveBaseImage(context.Background(), "arm64")
		if err == nil || !strings.Contains(err.Error(), wrong) || !strings.Contains(err.Error(), "target is linux/arm64") {
			t.Fatal(err)
		}
	}
}

// TestNoMatchingBase rejects an index that has no manifest for the target.
func TestNoMatchingBase(t *testing.T) {
	index, _ := imagePins(t, v1.Platform{OS: "linux", Architecture: "amd64"})
	settings := projectSettings{BaseImage: index}
	_, err := settings.resolveBaseImage(context.Background(), "arm64")
	if err == nil || !strings.Contains(err.Error(), "linux/arm64") {
		t.Fatal(err)
	}
}
