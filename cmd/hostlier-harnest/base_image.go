// Resolves the pinned Harnest base image for a Hostlier workload.
package main

import (
	"context"
	"fmt"
	"strings"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/remote"
)

// resolveBaseImage reads only registry metadata. The selected manifest digest is
// part of the reviewed plan and becomes the exact FROM used by the packager.
func (s projectSettings) resolveBaseImage(ctx context.Context, arch string) (string, error) {
	target := v1.Platform{OS: "linux", Architecture: arch}
	pin := s.BaseImage
	ref, err := name.NewDigest(pin)
	if err != nil {
		return "", fmt.Errorf("invalid Harnest base image reference %q", pin)
	}
	img, err := remote.Image(ref, remote.WithContext(ctx), remote.WithPlatform(target), remote.WithAuthFromKeychain(authn.DefaultKeychain))
	if err != nil {
		return "", fmt.Errorf("cannot resolve Harnest base image %s for %s; check registry access and pin a manifest or image index containing that platform", pin, target)
	}
	cfg, err := img.ConfigFile()
	if err != nil {
		return "", fmt.Errorf("cannot verify platform of Harnest base image %s for %s", pin, target)
	}
	// Single manifests bypass index selection, and even an index can mislabel a child.
	if cfg.OS != target.OS || cfg.Architecture != arch {
		return "", fmt.Errorf("Harnest base image %s is %s/%s; target is %s. Pin a matching image or a multi-platform index", pin, cfg.OS, cfg.Architecture, target)
	}
	digest, err := img.Digest()
	if err != nil {
		return "", fmt.Errorf("cannot verify Harnest base image manifest digest for %s", target)
	}
	repo, _, _ := strings.Cut(pin, "@")
	return repo + "@" + digest.String(), nil
}
