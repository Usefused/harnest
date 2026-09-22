package agentpack

import (
	"fmt"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"strings"
)

// storedPermissions avoids read-only hardlinks on Windows: removing one would
// otherwise require changing an attribute shared by every link to that object.
// Windows has no POSIX executable bits; content verification remains mandatory.
func storedPermissions(mode os.FileMode) os.FileMode {
	if runtime.GOOS == "windows" {
		return 0666
	}
	return mode
}

// sourcePermissions identifies Windows programs by extension, independent of ACLs.
func sourcePermissions(filename string, mode os.FileMode) os.FileMode {
	if runtime.GOOS != "windows" {
		return mode
	}
	switch strings.ToLower(filepath.Ext(filename)) {
	case ".exe", ".com", ".cmd", ".bat":
		return 0555
	default:
		return 0444
	}
}

// safeWindowsPath rejects aliases, device names and streams before extraction.
func safeWindowsPath(value string) bool {
	for _, part := range strings.Split(value, "/") {
		if !safeWindowsComponent(part) {
			return false
		}
	}
	return true
}

// safeWindowsComponent excludes names Win32 normalizes or interprets as devices.
func safeWindowsComponent(part string) bool {
	if strings.TrimRight(part, " .") != part || strings.ContainsAny(part, `<>:"\|?*`) {
		return false
	}
	for _, character := range part {
		if character < 32 {
			return false
		}
	}
	base, _, _ := strings.Cut(strings.ToUpper(part), ".")
	base = strings.TrimRight(base, " ")
	switch base {
	case "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$":
		return false
	}
	if strings.HasPrefix(base, "COM") || strings.HasPrefix(base, "LPT") {
		return !strings.Contains("|1|2|3|4|5|6|7|8|9|¹|²|³|", "|"+base[3:]+"|")
	}
	return true
}

// validateWindowsTree rules out case aliases and privileged symlink requirements.
func validateWindowsTree(files []File) error {
	names := make(map[string]string)
	for _, file := range files {
		if file.Link != "" || !SafePath(file.Path) || !safeWindowsPath(file.Path) {
			return fmt.Errorf("invalid Windows runtime path or link: %s", file.Path)
		}
		for name := file.Path; name != "."; name = path.Dir(name) {
			key := strings.ToUpper(name)
			if previous, ok := names[key]; ok && previous != name {
				return fmt.Errorf("Windows runtime paths alias: %s and %s", previous, name)
			}
			names[key] = name
		}
	}
	return nil
}
