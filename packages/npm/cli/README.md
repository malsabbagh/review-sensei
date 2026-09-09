# @reviewsensei/cli

This package is a small, shell-free launcher for the ReviewSensei native
standalone executable. It forwards the command line to the optional package
for the current supported operating-system/architecture pair; review logic
continues to live in the Python `review_sensei.cli:main` entry point bundled
by PyInstaller.

Requirements: Node.js 22 or newer and an external `git` executable when using
`prepare-diff`. The launcher never downloads a runtime, runs an install hook,
or contacts a provider. Install the exact version and invoke it with:

```console
npx --yes @reviewsensei/cli@0.1.1 --version
```

Supported targets are macOS arm64/x64, Linux arm64/x64 with glibc, and
Windows x64. Linux musl systems and other architectures fail with a bounded
unsupported-target error.
