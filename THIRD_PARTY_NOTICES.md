# Third-party notices

Harnest release binaries embed `uv` 0.12.6, copyright Astral Software Inc. and
contributors. Harnest redistributes `uv` under its MIT license; the complete
license text is included at `licenses/uv-LICENSE-MIT`.

When no compatible system Python is available, embedded `uv` downloads a
managed CPython distribution from Astral's `python-build-standalone` project.
That Python distribution is installed into the user's Harnest data directory
and carries its own license information; it is not embedded in or redistributed
inside the Harnest release archive.
