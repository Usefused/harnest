package main

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"net/mail"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"harnest.dev/harnest/internal/agentpack"
)

const buildReportFile = "harnest-build-report.json"

type packRequirementReason struct {
	Requirement string `json:"requirement"`
	Owner       string `json:"owner"`
}
type packDistributionReport struct {
	Name     string   `json:"name"`
	Version  string   `json:"version"`
	Size     int64    `json:"size"`
	Reasons  []string `json:"reasons"`
	Requires []string `json:"declaredRequires,omitempty"`
}
type runtimeBuildReport struct {
	Version           int                      `json:"version"`
	Kind              string                   `json:"kind"`
	Roots             []packRequirementReason  `json:"roots"`
	Packages          []packDistributionReport `json:"packages"`
	LogicalBytes      int64                    `json:"logicalBytes"`
	UniqueObjectBytes int64                    `json:"uniqueObjectBytes"`
	Files             []agentpack.File         `json:"files"`
}

// packRequirementReasons preserves declarations and owners independently of resolver deduplication.
func packRequirementReasons(plan runtimePackPlan) ([]packRequirementReason, error) {
	result := []packRequirementReason{}
	for _, bundle := range plan.Owners {
		dependencyPlan, err := inspectCompileDependencyPlan(bundle)
		if err != nil {
			return nil, err
		}
		for _, project := range dependencyPlan.ProjectFiles {
			values, err := projectRuntimeRequirements(project, "runtime report")
			if err != nil {
				return nil, err
			}
			relative, _ := filepath.Rel(bundle.Directory, project)
			result = appendRequirementReasons(result, values, filepath.Base(bundle.Directory)+"/"+filepath.ToSlash(relative))
		}
		result = appendRequirementReasons(result, dependencyPlan.CompileRequirements, filepath.Base(bundle.Directory)+"/"+compileSelectionFile+":extras")
	}
	result = appendRequirementReasons(result, []string{"harnest[" + strings.Join(plan.Requirements, ",") + "]"}, "Harnest compiler")
	return canonicalRequirementReasons(result), nil
}

// appendRequirementReasons retains distinct owners even when they request identical packages.
func appendRequirementReasons(result []packRequirementReason, values []string, owner string) []packRequirementReason {
	for _, value := range uniqueSorted(values) {
		result = append(result, packRequirementReason{value, owner})
	}
	return result
}

// makeRuntimeBuildReport distinguishes logical file bytes from unique stored object bytes.
func makeRuntimeBuildReport(staging string, plan runtimePackPlan, files []agentpack.File) (runtimeBuildReport, error) {
	result := runtimeBuildReport{Version: 1, Kind: "runtime", Files: files}
	roots, err := packRequirementReasons(plan)
	if err != nil {
		return result, err
	}
	result.Roots = roots
	result.Packages, err = installedPackDistributions(filepath.Join(staging, "packages"), roots)
	if err != nil {
		return result, err
	}
	seen := map[string]bool{}
	for _, file := range files {
		result.LogicalBytes += file.Size
		if file.Object != "" && !seen[file.Object] {
			result.UniqueObjectBytes += file.Size
			seen[file.Object] = true
		}
	}
	return result, nil
}

// installedPackDistributions reads installed wheel metadata without importing package code.
func installedPackDistributions(root string, reasons []packRequirementReason) ([]packDistributionReport, error) {
	paths, err := filepath.Glob(filepath.Join(root, "*.dist-info", "METADATA"))
	if err != nil {
		return nil, err
	}
	result := []packDistributionReport{}
	for _, path := range paths {
		item, err := installedDistributionReport(root, path, reasons)
		if err != nil {
			return nil, err
		}
		result = append(result, item)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Name < result[j].Name })
	return result, nil
}

// installedDistributionReport links installed versions to explicit roots or the resolved transitive closure.
func installedDistributionReport(root, path string, reasons []packRequirementReason) (packDistributionReport, error) {
	var result packDistributionReport
	file, err := os.Open(path)
	if err != nil {
		return result, err
	}
	defer file.Close()
	message, err := mail.ReadMessage(file)
	if err != nil {
		return result, err
	}
	result.Name, result.Version = message.Header.Get("Name"), message.Header.Get("Version")
	result.Requires = message.Header["Requires-Dist"]
	for _, reason := range reasons {
		if normalizedRequirementName(reason.Requirement) == normalizedRequirementName(result.Name) {
			result.Reasons = append(result.Reasons, reason.Owner+": "+reason.Requirement)
		}
	}
	if len(result.Reasons) == 0 {
		result.Reasons = []string{"required by resolved dependency closure"}
	}
	result.Size, err = installedDistributionSize(root, filepath.Join(filepath.Dir(path), "RECORD"))
	return result, err
}

// installedDistributionSize counts retained wheel files and never follows metadata outside the package target.
func installedDistributionSize(root, record string) (int64, error) {
	file, err := os.Open(record)
	if err != nil {
		return 0, err
	}
	defer file.Close()
	reader := csv.NewReader(file)
	reader.FieldsPerRecord = 3
	var size int64
	seen := map[string]bool{}
	for {
		row, err := reader.Read()
		if err == io.EOF {
			return size, nil
		}
		if err != nil {
			return 0, err
		}
		path := filepath.Join(root, filepath.FromSlash(row[0]))
		if !pathWithinDirectory(root, path) || seen[path] {
			continue
		}
		seen[path] = true
		info, err := os.Stat(path)
		if os.IsNotExist(err) {
			continue
		} // Installers may remove bytecode or relocate console wrappers.
		if err != nil {
			return 0, err
		}
		size += info.Size()
	}
}

// writeBuildReport uses stable JSON so equal reports participate in ordinary object deduplication.
func writeBuildReport(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return replaceRegularFileMode(path, append(data, '\n'), 0644)
}

// writeExecutableReport exposes the embedded source inventory and referenced runtime without copying dependencies.
func writeExecutableReport(output, artifact, runtimeRoot string, manifest agentpack.Manifest, embedded bool) error {
	source, err := os.ReadFile(filepath.Join(artifact, buildReportFile))
	if err != nil {
		return err
	}
	runtimeReport, err := os.ReadFile(filepath.Join(runtimeRoot, buildReportFile))
	if os.IsNotExist(err) {
		runtimeReport = []byte(`{"note":"runtime predates build reports"}`)
	} else if err != nil {
		return err
	}
	info, err := os.Stat(output)
	if err != nil {
		return err
	}
	report := map[string]any{"version": 1, "kind": "executable", "executableBytes": info.Size(), "embeddedRuntime": embedded, "runtimeDigest": manifest.Digest, "agent": json.RawMessage(source), "runtime": json.RawMessage(runtimeReport)}
	if err = writeBuildReport(output+".build-report.json", report); err != nil {
		return fmt.Errorf("write executable build report: %w", err)
	}
	return nil
}

// storeRuntimeBuildReport binds the reviewable report to the same content store as the runtime.
func storeRuntimeBuildReport(staging, store string, plan runtimePackPlan, manifest *agentpack.Manifest) error {
	report, err := makeRuntimeBuildReport(staging, plan, manifest.Files)
	if err != nil {
		return err
	}
	path := filepath.Join(staging, buildReportFile)
	if err = writeBuildReport(path, report); err != nil {
		return err
	}
	object, err := agentpack.StoreFile(path, store)
	if err != nil {
		return err
	}
	object.Path = buildReportFile
	manifest.Files = append(manifest.Files, object)
	return nil
}

// copyRuntimeBuildReport makes the bound report reviewable without materializing the runtime.
func copyRuntimeBuildReport(staging, pack string) error {
	data, err := os.ReadFile(filepath.Join(staging, buildReportFile))
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(pack, buildReportFile), data, 0644)
}

// linkPackObjects gives portable packs their own links without duplicating shared dependency bytes.
func linkPackObjects(store, pack string, files []agentpack.File) error {
	for _, file := range files {
		if file.Object == "" {
			continue
		}
		if err := agentpack.LinkObject(filepath.Join(store, file.Object), filepath.Join(pack, "objects", file.Object), file); err != nil {
			return err
		}
	}
	return nil
}

// canonicalRequirementReasons keeps runtime identity independent of repeated owners and CLI ordering.
func canonicalRequirementReasons(values []packRequirementReason) []packRequirementReason {
	seen := map[packRequirementReason]bool{}
	result := []packRequirementReason{}
	for _, value := range values {
		if !seen[value] {
			result = append(result, value)
			seen[value] = true
		}
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].Owner != result[j].Owner {
			return result[i].Owner < result[j].Owner
		}
		return result[i].Requirement < result[j].Requirement
	})
	return result
}
