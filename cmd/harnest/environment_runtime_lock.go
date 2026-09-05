package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"

	"harnest.dev/harnest/engine"
	"harnest.dev/harnest/internal/runtimewheel"
)

const (
	runtimeLockFormatLine = "# harnest-runtime-lock: 1"
	runtimeLockDigest     = "# harnest-input-sha256: "
	runtimeWheelMarker    = "__HARNEST_RELEASE_WHEEL_URI__"
	runtimeProjectMarker  = "__HARNEST_PROJECT_ROOT_URI__"
)

// prepareRuntimeLock resolves a reviewable lock or renders a prevalidated frozen lock.
func (a *application) prepareRuntimeLock(
	command *cobra.Command,
	bundle engine.Bundle,
	wheel runtimewheel.Artifact,
	staged stagedAgentEnvironment,
	plan runtimeDependencyPlan,
	frozen bool,
) (string, func(), error) {
	lockPath := filepath.Join(bundle.Directory, runtimeRequirementsLockFile)
	if !frozen {
		if err := a.resolveRuntimeLock(command, bundle, wheel, staged, plan, lockPath); err != nil {
			return "", nil, err
		}
	}
	rendered, cleanup, err := renderRuntimeLock(bundle, staged, lockPath)
	if err != nil {
		return "", nil, err
	}
	return rendered, cleanup, nil
}

// resolveRuntimeLock solves every runtime owner together, including the release wheel.
func (a *application) resolveRuntimeLock(
	command *cobra.Command,
	bundle engine.Bundle,
	wheel runtimewheel.Artifact,
	staged stagedAgentEnvironment,
	plan runtimeDependencyPlan,
	destination string,
) error {
	input, cleanupInput, err := createRuntimeLockInput(bundle, staged, plan)
	if err != nil {
		return err
	}
	defer cleanupInput()
	candidate, cleanupCandidate, err := createRuntimeLockTemporary(bundle, ".runtime-lock-candidate-*")
	if err != nil {
		return err
	}
	defer cleanupCandidate()
	arguments := []string{
		"pip", "compile", "--python", staged.python, "--generate-hashes", "--no-header", "--no-annotate",
		"--output-file", candidate, input,
	}
	arguments = append(arguments, plan.ProjectFiles...)
	if err := a.runRuntimeCommandWithEnvironment(
		command, staged.environment, staged.uvPath, arguments...,
	); err != nil {
		return fmt.Errorf("resolve complete runtime dependency lock: %w", err)
	}
	document, err := normalizeRuntimeLock(bundle, wheel, plan, candidate, staged.wheelPath)
	if err != nil {
		return err
	}
	if err := ensureReplaceableRuntimeLock(destination); err != nil {
		return err
	}
	if err := publishRuntimeLockDocument(destination, document); err != nil {
		return fmt.Errorf("publish runtime dependency lock: %w", err)
	}
	return nil
}

// createRuntimeLockInput owns compiler-injected requirements absent from authored projects.
func createRuntimeLockInput(
	bundle engine.Bundle, staged stagedAgentEnvironment, plan runtimeDependencyPlan,
) (string, func(), error) {
	wheelURI := runtimeWheelURI(staged.wheelPath)
	lines := []string{fmt.Sprintf(
		"harnest[%s] @ %s", bundle.Config.Spec.Framework.Name, wheelURI,
	)}
	pin, err := lockedFrameworkRequirement(bundle)
	if err != nil {
		return "", nil, err
	}
	if pin != "" {
		lines = append(lines, pin)
	}
	if plan.HasTasks {
		lines = append(lines, procrastinateRequirement)
	}
	path, cleanup, err := createRuntimeLockTemporary(bundle, ".runtime-lock-input-*")
	if err != nil {
		return "", nil, err
	}
	if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
		cleanup()
		return "", nil, fmt.Errorf("write complete runtime dependency input: %w", err)
	}
	return path, cleanup, nil
}

// createRuntimeLockTemporary confines generated resolver material to disposable state.
func createRuntimeLockTemporary(bundle engine.Bundle, pattern string) (string, func(), error) {
	file, err := os.CreateTemp(filepath.Join(bundle.Directory, ".harnest"), pattern)
	if err != nil {
		return "", nil, fmt.Errorf("create runtime dependency temporary file: %w", err)
	}
	path := file.Name()
	if err := file.Close(); err != nil {
		_ = os.Remove(path)
		return "", nil, fmt.Errorf("close runtime dependency temporary file: %w", err)
	}
	return path, func() { _ = os.Remove(path) }, nil
}

// normalizeRuntimeLock removes the machine-local wheel path and binds source inputs.
func normalizeRuntimeLock(
	bundle engine.Bundle,
	wheel runtimewheel.Artifact,
	plan runtimeDependencyPlan,
	candidate, wheelPath string,
) ([]byte, error) {
	contents, err := readRegularDependencyFile(candidate)
	if err != nil {
		return nil, fmt.Errorf("read resolved runtime dependency lock: %w", err)
	}
	wheelURI := runtimeWheelURI(wheelPath)
	if strings.Count(string(contents), wheelURI) != 1 {
		return nil, fmt.Errorf("resolved runtime lock did not contain exactly one embedded Harnest wheel")
	}
	body := strings.Replace(string(contents), wheelURI, runtimeWheelMarker, 1)
	// Relative direct references must survive a checkout moving between machines.
	projectPrefix := strings.TrimSuffix(runtimeWheelURI(bundle.Directory), "/") + "/"
	body = strings.ReplaceAll(body, projectPrefix, runtimeProjectMarker+"/")
	digest, err := runtimeLockInputFingerprint(bundle, wheel, plan)
	if err != nil {
		return nil, err
	}
	return []byte(runtimeLockFormatLine + "\n" + runtimeLockDigest + digest + "\n" + body), nil
}

// runtimeLockInputFingerprint detects stale locks without performing a fresh resolution.
func runtimeLockInputFingerprint(
	bundle engine.Bundle, wheel runtimewheel.Artifact, plan runtimeDependencyPlan,
) (string, error) {
	digest := sha256.New()
	for _, value := range []string{
		"1", bundle.Config.Spec.Runtime.Version, bundle.Config.Spec.Framework.Name,
		bundle.Config.Spec.Framework.EffectiveMode(), wheel.Name,
	} {
		digest.Write([]byte(value))
		digest.Write([]byte{0})
	}
	digest.Write(wheel.Contents)
	for _, path := range plan.ProjectFiles {
		if err := hashEnvironmentDependencyInput(digest, bundle.Directory, path, false); err != nil {
			return "", err
		}
	}
	if err := hashEnvironmentDependencyInput(
		digest, bundle.Directory, filepath.Join(bundle.Directory, "harnest.lock"), true,
	); err != nil {
		return "", err
	}
	digest.Write([]byte{0, byte(boolByte(plan.HasTasks))})
	return hex.EncodeToString(digest.Sum(nil)), nil
}

// validateFrozenRuntimeLock rejects missing or source-stale committed resolution.
func validateFrozenRuntimeLock(
	bundle engine.Bundle,
	wheel runtimewheel.Artifact,
	plan runtimeDependencyPlan,
	path string,
) error {
	contents, err := readRegularDependencyFile(path)
	if err != nil {
		return fmt.Errorf("frozen runtime dependency lock is unavailable: %w", err)
	}
	digest, _, err := splitRuntimeLock(contents)
	if err != nil {
		return fmt.Errorf("frozen runtime dependency lock is invalid: %w", err)
	}
	expected, err := runtimeLockInputFingerprint(bundle, wheel, plan)
	if err != nil {
		return err
	}
	if digest != expected {
		return fmt.Errorf("runtime dependency lock is stale; run harnest env sync without --frozen")
	}
	return nil
}

// splitRuntimeLock validates Harnest metadata and returns the resolver-owned body.
func splitRuntimeLock(contents []byte) (string, string, error) {
	text := string(contents)
	firstEnd := strings.IndexByte(text, '\n')
	if firstEnd < 0 || text[:firstEnd] != runtimeLockFormatLine {
		return "", "", fmt.Errorf("unsupported or missing lock format")
	}
	remainder := text[firstEnd+1:]
	secondEnd := strings.IndexByte(remainder, '\n')
	if secondEnd < 0 || !strings.HasPrefix(remainder[:secondEnd], runtimeLockDigest) {
		return "", "", fmt.Errorf("missing input fingerprint")
	}
	digest := strings.TrimPrefix(remainder[:secondEnd], runtimeLockDigest)
	if len(digest) != sha256.Size*2 {
		return "", "", fmt.Errorf("invalid input fingerprint")
	}
	if _, err := hex.DecodeString(digest); err != nil {
		return "", "", fmt.Errorf("invalid input fingerprint")
	}
	body := remainder[secondEnd+1:]
	if strings.Count(body, runtimeWheelMarker) != 1 {
		return "", "", fmt.Errorf("lock must contain exactly one Harnest release wheel marker")
	}
	return digest, body, nil
}

// renderRuntimeLock substitutes only the trusted, hash-bound release wheel location.
func renderRuntimeLock(
	bundle engine.Bundle, staged stagedAgentEnvironment, source string,
) (string, func(), error) {
	contents, err := readRegularDependencyFile(source)
	if err != nil {
		return "", nil, fmt.Errorf("read runtime dependency lock: %w", err)
	}
	_, body, err := splitRuntimeLock(contents)
	if err != nil {
		return "", nil, fmt.Errorf("read runtime dependency lock: %w", err)
	}
	rendered := strings.Replace(body, runtimeWheelMarker, runtimeWheelURI(staged.wheelPath), 1)
	projectPrefix := strings.TrimSuffix(runtimeWheelURI(bundle.Directory), "/")
	rendered = strings.ReplaceAll(rendered, runtimeProjectMarker, projectPrefix)
	path, cleanup, err := createRuntimeLockTemporary(bundle, ".runtime-lock-install-*")
	if err != nil {
		return "", nil, err
	}
	if err := os.WriteFile(path, []byte(rendered), 0o600); err != nil {
		cleanup()
		return "", nil, fmt.Errorf("render runtime dependency lock: %w", err)
	}
	return path, cleanup, nil
}

// refreshRuntimeLockMetadata accounts for the framework pin recorded after first install.
func refreshRuntimeLockMetadata(
	bundle engine.Bundle, wheel runtimewheel.Artifact, plan runtimeDependencyPlan,
) error {
	path := filepath.Join(bundle.Directory, runtimeRequirementsLockFile)
	contents, err := readRegularDependencyFile(path)
	if err != nil {
		return fmt.Errorf("read runtime dependency lock for finalization: %w", err)
	}
	_, body, err := splitRuntimeLock(contents)
	if err != nil {
		return fmt.Errorf("finalize runtime dependency lock: %w", err)
	}
	digest, err := runtimeLockInputFingerprint(bundle, wheel, plan)
	if err != nil {
		return err
	}
	document := []byte(runtimeLockFormatLine + "\n" + runtimeLockDigest + digest + "\n" + body)
	if err := publishRuntimeLockDocument(path, document); err != nil {
		return fmt.Errorf("finalize runtime dependency lock: %w", err)
	}
	return nil
}

// ensureReplaceableRuntimeLock refuses to overwrite links or non-file paths.
func ensureReplaceableRuntimeLock(path string) error {
	_, err := regularDependencyPathExists(path, "runtime dependency lock")
	return err
}

// publishRuntimeLockDocument leaves committed lock material readable by repository tooling.
func publishRuntimeLockDocument(path string, contents []byte) error {
	return replaceRegularFileMode(path, contents, 0o644)
}

// runtimeWheelURI renders an absolute local path without treating a Windows drive as a host.
func runtimeWheelURI(path string) string {
	slashPath := filepath.ToSlash(path)
	if filepath.VolumeName(path) != "" && !strings.HasPrefix(slashPath, "/") {
		slashPath = "/" + slashPath
	}
	return (&url.URL{Scheme: "file", Path: slashPath}).String()
}
