// Package pysource holds the Python files the Harnest CLI writes into agents or runs, kept as real files under files/.
//
// A file ending in .py is used exactly as written. A file ending in .py.tmpl is
// a Go text/template: the CLI fills its {{.Field}} placeholders when it writes
// the file. See README.md for the template helpers.
package pysource

import (
	"embed"
	"fmt"
	"io/fs"
	"path"
	"sort"
	"strconv"
	"strings"
	"text/template"
)

// all: keeps names that start with an underscore, such as __init__.py and _example.py.
//
//go:embed all:files
var embedded embed.FS

const templateSuffix = ".tmpl"

var (
	sources   = map[string]string{}
	templates = template.New("pysource").Option("missingkey=error").Funcs(template.FuncMap{
		// quote writes a double-quoted Python string, the same text as Go's %q.
		"quote": strconv.Quote,
		// words turns snake_case into spaced words for docstrings.
		"words": func(name string) string { return strings.ReplaceAll(name, "_", " ") },
		// file inserts another plain .py file from files/.
		"file": File,
	})
)

func init() {
	err := fs.WalkDir(embedded, "files", func(name string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if ignored(name, entry.IsDir()) {
			if entry.IsDir() {
				return fs.SkipDir
			}
			return nil
		}
		if entry.IsDir() {
			return nil
		}
		data, err := embedded.ReadFile(name)
		if err != nil {
			return err
		}
		relative := strings.TrimPrefix(name, "files/")
		// Like a Go raw string, ignore carriage returns so a CRLF checkout writes the same files.
		source := strings.ReplaceAll(string(data), "\r", "")
		sources[relative] = source
		if strings.HasSuffix(relative, templateSuffix) {
			_, err = templates.New(relative).Parse(source)
		}
		return err
	})
	if err != nil {
		panic(fmt.Sprintf("load embedded Python sources: %v", err))
	}
}

// ignored reports local clutter that all: would otherwise embed: hidden files
// such as .DS_Store, and Python bytecode an editor or interpreter left behind.
// Without this, init would write those files into new agents.
func ignored(name string, dir bool) bool {
	base := path.Base(name)
	if dir {
		return base == "__pycache__" || (strings.HasPrefix(base, ".") && name != "files")
	}
	return strings.HasPrefix(base, ".") || strings.HasSuffix(base, ".pyc")
}

// File returns a plain .py file from files/ exactly as written.
func File(name string) string {
	source, ok := sources[name]
	if !ok || strings.HasSuffix(name, templateSuffix) {
		panic(fmt.Sprintf("pysource: no plain Python file %q", name))
	}
	return source
}

// Render fills a .py.tmpl file from files/ with data. Every placeholder must
// have a value, so a missing field fails tests instead of writing broken Python.
func Render(name string, data map[string]any) string {
	if path.Ext(name) != templateSuffix {
		panic(fmt.Sprintf("pysource: %q is not a template", name))
	}
	var output strings.Builder
	if err := templates.ExecuteTemplate(&output, name, data); err != nil {
		panic(fmt.Sprintf("pysource: render %s: %v", name, err))
	}
	return output.String()
}

// Names lists the files under a folder of files/, such as "init/frameworks/adk/advanced/", in sorted order.
func Names(folder string) []string {
	var names []string
	for name := range sources {
		if strings.HasPrefix(name, folder) {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names
}

// Source returns a file from files/: a .py.tmpl file filled with data, any other file as written.
func Source(name string, data map[string]any) string {
	if strings.HasSuffix(name, templateSuffix) {
		return Render(name, data)
	}
	return File(name)
}
