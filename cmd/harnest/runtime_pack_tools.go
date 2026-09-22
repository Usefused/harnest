package main

import (
	"archive/zip"
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"

	"github.com/spf13/cobra"
	"harnest.dev/harnest/internal/agentpack"
)

// relocatePackTools replaces install-time interpreter references before hashing.
func (a *application) relocatePackTools(command *cobra.Command, packages, python string) error {
	if runtime.GOOS != "windows" {
		return relocatePackScripts(packages, python)
	}
	launcher, err := loadAgentLauncher(command)
	if err != nil {
		return err
	}
	directory := filepath.Join(packages, agentpack.ScriptsDirectory)
	entries, err := filepath.Glob(filepath.Join(directory, "*.exe"))
	if err != nil {
		return err
	}
	for _, filename := range entries {
		if err := relocateWindowsTool(filename, launcher); err != nil {
			return err
		}
	}
	return nil
}

// relocateWindowsTool extracts installer ZIP entrypoints without depending on
// uv's private PE resources or embedded absolute interpreter path. Native tools
// are left intact. Every Python tool shares identical launcher bytes, with its
// small script stored separately so deduplication spans commands and packs.
func relocateWindowsTool(filename string, launcher []byte) error {
	script, err := windowsToolScript(filename)
	if err != nil || script == nil {
		return err
	}
	if err = os.WriteFile(filename+agentpack.ConsoleScriptSuffix, script, 0644); err != nil {
		return err
	}
	return os.WriteFile(filename, launcher, 0755)
}

// windowsToolScript recognizes Python zipapp entrypoints while preserving real PE tools.
func windowsToolScript(filename string) ([]byte, error) {
	archive, err := zip.OpenReader(filename)
	if errors.Is(err, zip.ErrFormat) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer archive.Close()
	script, err := archive.Open("__main__.py")
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer script.Close()
	data, err := io.ReadAll(io.LimitReader(script, (16<<20)+1))
	if err != nil {
		return nil, err
	}
	if len(data) > 16<<20 {
		return nil, fmt.Errorf("console script too large: %s", filename)
	}
	return stripScriptShebang(data), nil
}

// stripScriptShebang removes a machine-specific path Python no longer needs.
func stripScriptShebang(data []byte) []byte {
	if bytes.HasPrefix(data, []byte("#!")) {
		if line := bytes.IndexByte(data, '\n'); line >= 0 {
			return data[line+1:]
		}
	}
	return data
}
