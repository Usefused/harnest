package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"harnest.dev/harnest/engine"
	"harnest.dev/harnest/internal/runtimewheel"
)

// packLockIdentity binds existing committed production pins to the agent's runtime attachment.
func packLockIdentity(bundle engine.Bundle) (string, error) {
	data, err := readRegularDependencyFile(filepath.Join(bundle.Directory, runtimeRequirementsLockFile))
	if os.IsNotExist(err) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]), nil
}

// packLockConstraints preserves existing production pins while resolving one shared closure.
func packLockConstraints(plan runtimePackPlan, staging, wheelPath string) ([]string, error) {
	contents, err := os.ReadFile(wheelPath)
	if err != nil {
		return nil, err
	}
	wheel := runtimewheel.Artifact{Name: filepath.Base(wheelPath), Contents: contents}
	var paths []string
	for index, bundle := range plan.Owners {
		body, err := packOwnerConstraint(bundle, wheel, wheelPath)
		if err != nil {
			return nil, err
		}
		if body == "" {
			continue
		}
		path := filepath.Join(staging, fmt.Sprintf("agent-%d.constraints", index))
		if err := os.WriteFile(path, []byte(body), 0600); err != nil {
			return nil, err
		}
		paths = append(paths, path)
	}
	return paths, nil
}

// packOwnerConstraint refuses stale locks rather than silently changing deployed dependency pins.
func packOwnerConstraint(bundle engine.Bundle, wheel runtimewheel.Artifact, wheelPath string) (string, error) {
	filename := filepath.Join(bundle.Directory, runtimeRequirementsLockFile)
	data, err := readRegularDependencyFile(filename)
	if os.IsNotExist(err) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	plan, err := inspectRuntimeDependencyPlan(bundle)
	if err != nil {
		return "", err
	}
	if err = validateFrozenRuntimeLock(bundle, wheel, plan, runtimeEnvironmentProfile, filename); err != nil {
		return "", err
	}
	_, body, err := splitRuntimeLock(data)
	if err != nil {
		return "", err
	}
	body = strings.Replace(body, runtimeWheelMarker, runtimeWheelURI(wheelPath), 1)
	return strings.ReplaceAll(body, runtimeProjectMarker, strings.TrimSuffix(runtimeWheelURI(bundle.Directory), "/")), nil
}
