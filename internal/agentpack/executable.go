package agentpack

import (
	"archive/zip"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const executableFormat = "harnest.executable/v1"
const executableManifest = "executable.json"

// Executable binds a small native launcher to exactly one dependency pack.
type Executable struct {
	Format      string            `json:"format"`
	Runtime     Reference         `json:"runtime"`
	Embedded    bool              `json:"embedded"`
	Environment map[string]string `json:"environment,omitempty"`
}

// Reference keeps attached executables small regardless of dependency inventory size.
type Reference struct {
	Digest string `json:"digest"`
	OS     string `json:"os"`
	Arch   string `json:"arch"`
}

// CheckHost rejects incompatible payloads before extraction.
func (r Reference) CheckHost() error { return (Manifest{OS: r.OS, Arch: r.Arch}).CheckHost() }

// WriteExecutable appends only agent files unless embedding is explicitly requested.
func WriteExecutable(output string, launcher []byte, artifact, pack string, m Manifest, embed bool, environment map[string]string) error {
	if err := m.Validate(); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(output), 0755); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(output), ".agent-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	if _, err = file.Write(launcher); err != nil {
		return err
	}
	if err = appendPayload(file, artifact, pack, m, embed, environment); err != nil {
		return err
	}
	if err = extendMachO(file); err != nil {
		return err
	}
	if err = file.Chmod(0755); err != nil {
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Rename(file.Name(), output)
}

// appendPayload uses one archive layout for attached and embedded executables.
func appendPayload(file *os.File, artifact, pack string, m Manifest, embed bool, environment map[string]string) error {
	var err error
	archive := zip.NewWriter(file)
	metadata, _ := json.Marshal(Executable{Format: executableFormat, Runtime: Reference{m.Digest, m.OS, m.Arch}, Embedded: embed, Environment: environment})
	if err = writeZipBytes(archive, executableManifest, metadata); err != nil {
		return err
	}
	if err = writeZipTree(archive, artifact, "agent"); err != nil {
		return err
	}
	if embed {
		if err = writeZipTree(archive, pack, "runtime"); err != nil {
			return err
		}
	}
	if err = archive.Close(); err != nil {
		return err
	}
	return nil
}

// writeZipBytes keeps generated metadata deterministic and bounded.
func writeZipBytes(archive *zip.Writer, name string, data []byte) error {
	writer, err := archive.Create(name)
	if err != nil {
		return err
	}
	_, err = writer.Write(data)
	return err
}

// writeZipTree preserves executable tools without allowing symlinks in agent archives.
func writeZipTree(archive *zip.Writer, root, prefix string) error {
	return filepath.WalkDir(root, func(filename string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("executable payload must contain regular files: %s", filename)
		}
		relative, err := filepath.Rel(root, filename)
		if err != nil {
			return err
		}
		header := &zip.FileHeader{Name: prefix + "/" + filepath.ToSlash(relative), Method: zip.Deflate}
		header.SetMode(info.Mode().Perm())
		writer, err := archive.CreateHeader(header)
		if err != nil {
			return err
		}
		input, err := os.Open(filename)
		if err != nil {
			return err
		}
		defer input.Close()
		_, err = io.Copy(writer, input)
		return err
	})
}

// Archive owns the native file while exposing its appended ZIP payload.
type Archive struct {
	*zip.Reader
	file *os.File
}

// Close releases the executable file after extraction.
func (a *Archive) Close() error { return a.file.Close() }

// openArchive reads ZIP bytes before any native code signature trailer.
func openArchive(filename string) (*Archive, error) {
	file, err := os.Open(filename)
	if err != nil {
		return nil, err
	}
	end, err := executablePayloadEnd(file)
	if err != nil {
		file.Close()
		return nil, err
	}
	reader, err := zip.NewReader(file, end)
	if err != nil {
		file.Close()
		return nil, err
	}
	return &Archive{reader, file}, nil
}

// ReadExecutable reads the appended ZIP without executing or loading authored code.
func ReadExecutable(filename string) (*Archive, Executable, error) {
	var metadata Executable
	archive, err := openArchive(filename)
	if err != nil {
		return nil, metadata, fmt.Errorf("open agent payload: %w", err)
	}
	file, err := archive.Open(executableManifest)
	if err != nil {
		archive.Close()
		return nil, metadata, err
	}
	defer file.Close()
	err = json.NewDecoder(io.LimitReader(file, 64<<20)).Decode(&metadata)
	if err == nil && metadata.Format != executableFormat {
		err = fmt.Errorf("unsupported executable format")
	}
	if err == nil {
		_, err = objectMode(metadata.Runtime.Digest + "-444")
	}
	if err != nil {
		archive.Close()
		return nil, metadata, err
	}
	return archive, metadata, nil
}

// ExtractPrefix expands into a fresh private directory with no traversal or overwrites.
func ExtractPrefix(archive *Archive, prefix, destination string) error {
	for _, file := range archive.File {
		if !strings.HasPrefix(file.Name, prefix+"/") {
			continue
		}
		relative := strings.TrimPrefix(file.Name, prefix+"/")
		if !SafePath(relative) || !file.Mode().IsRegular() {
			return fmt.Errorf("unsafe executable payload path %q", file.Name)
		}
		if err := extractFile(file, filepath.Join(destination, filepath.FromSlash(relative))); err != nil {
			return err
		}
	}
	return nil
}

// extractFile uses exclusive creation so duplicate entries cannot replace prior bytes.
func extractFile(file *zip.File, target string) error {
	if err := os.MkdirAll(filepath.Dir(target), 0700); err != nil {
		return err
	}
	input, err := file.Open()
	if err != nil {
		return err
	}
	defer input.Close()
	output, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(output, input)
	modeErr := output.Chmod(file.Mode().Perm() & 0777)
	closeErr := output.Close()
	for _, err := range []error{copyErr, modeErr, closeErr} {
		if err != nil {
			return err
		}
	}
	return nil
}
