package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

const vscodeInterpreterKey = "python.defaultInterpreterPath"

// syncVSCodeInterpreterSettings records the explicitly synchronized interpreter, independently of .venv.
func syncVSCodeInterpreterSettings(project, python string) (string, error) {
	directory := filepath.Join(project, ".vscode")
	if err := ensureRegularPrivateDirectory(directory, "VS Code settings directory"); err != nil {
		return "", err
	}
	path := filepath.Join(directory, "settings.json")
	contents, mode, err := readVSCodeSettings(path)
	if err != nil {
		return "", err
	}
	relative, err := filepath.Rel(project, python)
	if err != nil {
		return "", fmt.Errorf("resolve VS Code interpreter: %w", err)
	}
	interpreter := "${workspaceFolder}/" + filepath.ToSlash(relative)
	updated, changed, err := setVSCodeInterpreter(contents, interpreter)
	if err != nil {
		return "", fmt.Errorf("read VS Code settings %s: %w", path, err)
	}
	if changed {
		if err := writeVSCodeSettings(path, updated, mode); err != nil {
			return "", err
		}
	}
	return path, nil
}

// readVSCodeSettings rejects links and bounds editor input before updating it.
func readVSCodeSettings(path string) ([]byte, os.FileMode, error) {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return []byte("{}\n"), 0o644, nil
	}
	if err != nil {
		return nil, 0, fmt.Errorf("inspect VS Code settings: %w", err)
	}
	if !info.Mode().IsRegular() || info.Size() > 1<<20 {
		return nil, 0, fmt.Errorf("VS Code settings must be a regular file under 1 MiB: %s", path)
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return nil, 0, fmt.Errorf("read VS Code settings: %w", err)
	}
	return contents, info.Mode().Perm(), nil
}

// setVSCodeInterpreter updates only the interpreter value, preserving unrelated JSONC settings.
func setVSCodeInterpreter(contents []byte, interpreter string) ([]byte, bool, error) {
	masked, err := maskVSCodeComments(contents)
	if err != nil {
		return nil, false, err
	}
	var settings map[string]json.RawMessage
	if err := json.Unmarshal(maskVSCodeTrailingCommas(masked), &settings); err != nil || settings == nil {
		return nil, false, fmt.Errorf("settings.json must contain a JSON object")
	}
	if _, exists := settings[vscodeInterpreterKey]; exists {
		return replaceVSCodeInterpreter(contents, maskVSCodeTrailingCommas(masked), interpreter)
	}
	closing := bytes.LastIndexByte(masked, '}')
	before := bytes.TrimSpace(masked[:closing])
	separator := ","
	if before[len(before)-1] == '{' || before[len(before)-1] == ',' {
		separator = ""
	}
	value, _ := json.Marshal(interpreter)
	addition := []byte(fmt.Sprintf("%s\n  %q: %s\n", separator, vscodeInterpreterKey, value))
	updated := append(append(append([]byte{}, contents[:closing]...), addition...), contents[closing:]...)
	return updated, true, nil
}

// replaceVSCodeInterpreter uses decoder offsets to preserve comments and unrelated settings verbatim.
// Updating every duplicate top-level key prevents a later stale value from winning in VS Code.
func replaceVSCodeInterpreter(contents, masked []byte, interpreter string) ([]byte, bool, error) {
	decoder := json.NewDecoder(bytes.NewReader(masked))
	if _, err := decoder.Token(); err != nil {
		return nil, false, err
	}
	replacement, _ := json.Marshal(interpreter)
	var output bytes.Buffer
	copied := 0
	for decoder.More() {
		key, err := decoder.Token()
		if err != nil {
			return nil, false, err
		}
		var value json.RawMessage
		if err := decoder.Decode(&value); err != nil {
			return nil, false, err
		}
		if key != vscodeInterpreterKey {
			continue
		}
		var current string
		if json.Unmarshal(value, &current) == nil && current == interpreter {
			continue
		}
		end := int(decoder.InputOffset())
		start := end - len(value)
		output.Write(contents[copied:start])
		output.Write(replacement)
		copied = end
	}
	if copied == 0 {
		return contents, false, nil
	}
	output.Write(contents[copied:])
	return output.Bytes(), true, nil
}

// maskVSCodeComments blanks JSONC comments while retaining offsets and line breaks.
func maskVSCodeComments(source []byte) ([]byte, error) {
	masked := bytes.Clone(source)
	for index := 0; index < len(masked); {
		switch {
		case masked[index] == '"':
			index = endVSCodeString(masked, index)
		case index+1 < len(masked) && masked[index] == '/' && masked[index+1] == '/':
			index = maskVSCodeLineComment(masked, index)
		case index+1 < len(masked) && masked[index] == '/' && masked[index+1] == '*':
			next, err := maskVSCodeBlockComment(masked, index)
			if err != nil {
				return nil, err
			}
			index = next
		default:
			index++
		}
	}
	return masked, nil
}

// maskVSCodeLineComment retains the next line's offset in the editor file.
func maskVSCodeLineComment(source []byte, start int) int {
	for start < len(source) && source[start] != '\n' {
		source[start] = ' '
		start++
	}
	return start
}

// maskVSCodeBlockComment rejects an unfinished comment before any file write.
func maskVSCodeBlockComment(source []byte, start int) (int, error) {
	end := bytes.Index(source[start+2:], []byte("*/"))
	if end < 0 {
		return 0, fmt.Errorf("unterminated VS Code settings comment")
	}
	limit := start + end + 4
	for start < limit {
		if source[start] != '\n' {
			source[start] = ' '
		}
		start++
	}
	return start, nil
}

// endVSCodeString skips a quoted JSON token without mistaking its slashes for comments.
func endVSCodeString(source []byte, start int) int {
	for index := start + 1; index < len(source); index++ {
		if source[index] == '\\' {
			index++
			continue
		}
		if source[index] == '"' {
			return index + 1
		}
	}
	return len(source)
}

// maskVSCodeTrailingCommas makes ordinary VS Code JSONC readable by encoding/json.
func maskVSCodeTrailingCommas(source []byte) []byte {
	masked := bytes.Clone(source)
	for index := 0; index < len(masked); index++ {
		if masked[index] == '"' {
			index = endVSCodeString(masked, index) - 1
			continue
		}
		if masked[index] != ',' {
			continue
		}
		next := nextVSCodeToken(masked, index+1)
		if next == '}' || next == ']' {
			masked[index] = ' '
		}
	}
	return masked
}

// nextVSCodeToken skips JSON whitespace when checking a possible trailing comma.
func nextVSCodeToken(source []byte, index int) byte {
	for index < len(source) {
		switch source[index] {
		case ' ', '\n', '\r', '\t':
			index++
		default:
			return source[index]
		}
	}
	return 0
}

// writeVSCodeSettings publishes a complete editor file without following an existing link.
func writeVSCodeSettings(path string, contents []byte, mode os.FileMode) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".settings.harnest-*")
	if err != nil {
		return fmt.Errorf("stage VS Code settings: %w", err)
	}
	defer os.Remove(file.Name())
	if err := file.Chmod(mode); err != nil {
		_ = file.Close()
		return fmt.Errorf("set VS Code settings permissions: %w", err)
	}
	if _, err := file.Write(contents); err != nil {
		_ = file.Close()
		return fmt.Errorf("write VS Code settings: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close VS Code settings: %w", err)
	}
	if err := os.Rename(file.Name(), path); err != nil {
		return fmt.Errorf("publish VS Code settings: %w", err)
	}
	return nil
}
