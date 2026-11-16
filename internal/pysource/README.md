# Python source files

This folder holds the Python that the `harnest` CLI writes into agents or runs
itself, kept as real files rather than Go strings. Go embeds `files/` into the
binary at build time, so to change one, edit the file and rebuild.

| Folder | Written or run by |
| --- | --- |
| `files/init/` | `harnest init`: the starter agent, its `examples/` (written with `--example`), the advanced-mode test in `advanced/`, and `shared/` snippets also used in the folder guides |
| `files/init/frameworks/<name>/` | What differs by framework: `advanced/` holds the files `--mode advanced` writes, and the optional `starter/` replaces shared starter files in every mode. Every file in these folders is written, so adding a file needs no Go change |
| `files/managed/` | The minimal managed agent shared by `harnest init`, agent templates and Hostlier (`internal/agentscaffold`) |
| `files/add/` | `harnest add` for tools, subagents, tasks, lifecycle hooks, context providers, MCP clients, cron jobs and channels, plus custom metrics from `harnest add eval` |
| `files/extension/` | `harnest extensions init` |
| `files/pack/` | `harnest pack init` |
| `files/probes/` | Scripts the CLI runs with `python -c` (`harnest doctor`, the environment check) |

To find where a file is used, search the Go code for its path, for example
`pysource.Render("add/tool.py.tmpl"` or `pysource.File("init/tools/echo.py")`.
Files under `files/init/frameworks/` are read by folder instead. Hidden files and
`__pycache__` folders are never embedded, so local clutter cannot reach new agents.

## Plain files and templates

- **`.py`** files are written exactly as they are.
- **`.py.tmpl`** files are Go [text/template](https://pkg.go.dev/text/template)
  files. The CLI fills each `{{.Field}}` from the values the Go caller passes.
  Every field a template uses must be passed. If one is missing, `go test`
  fails instead of a broken file being written.

Three helpers are available in templates:

| Helper | Example | Produces |
| --- | --- | --- |
| `quote` | `name={{quote .Name}}` | A double-quoted Python string, the same text as Go's `%q` |
| `words` | `"""Handle one {{words .Name}} request."""` | `lookup_order` becomes `lookup order` |
| `file` | `{{file "init/shared/cron_daily_report.py"}}` | Another plain `.py` file from `files/`, inserted as is |

`{{-` and `-}}` remove the whitespace next to a placeholder. Use `{{.Source -}}`
at the end of a file so its final newline is not added twice. Python braces
right before a placeholder need a space and a trim, as in `add/mcp.py.tmpl`:
`headers={ {{- quote .TokenHeader}}: ...}` writes `headers={"Authorization": ...}`.
Writing `{{{` would not parse.

## Changing what agents get

These files are what new agents start with, so a change here changes every
agent created afterwards. Existing agents are not touched. Run
`go test ./cmd/harnest ./internal/...` to check the scaffolds, and add a line
under `## Unreleased` in `CHANGELOG.md` when users can notice the change.
