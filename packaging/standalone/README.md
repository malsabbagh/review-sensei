# Native standalone builds

The release workflow builds one-folder executables with the exact PyInstaller
version in `requirements.txt` on a native runner for each supported target.
Linux targets build inside a pinned Python 3.11 Debian 12 container and then
run a second smoke test in a minimal Debian 12 consumer container (glibc 2.36).
This keeps the bundled Python compatible with that baseline; the macOS and
Windows targets still build directly on native runners.
The entrypoint remains `review_sensei.cli:main`; the bundle includes the
package's JSON defaults and distribution metadata so `--version` works when
Python is absent from `PATH`.

Build output is staging only and belongs under an ignored `build/` or `dist/`
directory. The builder does not cross-compile, download a Python runtime, or
invoke a shell. The resulting folder is copied into the matching npm platform
package by `scripts/prepare_npm_packages.py` and independently validated before
`npm pack`.

For a local deterministic prerequisite check (which does not require
PyInstaller), run:

```console
python scripts/build_standalone.py --check
```
