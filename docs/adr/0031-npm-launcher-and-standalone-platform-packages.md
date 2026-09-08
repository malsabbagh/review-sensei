# ADR 0031: npm launcher and standalone platform packages

- Status: Proposed
- Date: 2026-09-07
- Related issue: internal tracking issue #103

## Context

ReviewSensei's Python distribution is the provider-neutral review engine and
must remain usable from GitHub Actions and local environments. Consumers also
need a convenient `npx` command that does not require a system Python
installation. The npm surface therefore introduces a package boundary and a
native-build supply-chain boundary without moving review logic into JavaScript.

## Decision

Publish a thin `@reviewsensei/cli` launcher with exactly five optional,
exact-version platform packages:

| Target | Node platform/arch | Package | Payload |
| --- | --- | --- | --- |
| macOS Apple Silicon | darwin/arm64 | `@reviewsensei/cli-darwin-arm64` | `bin/review-sensei` |
| macOS Intel | darwin/x64 | `@reviewsensei/cli-darwin-x64` | `bin/review-sensei` |
| Linux glibc ARM64 | linux/arm64 | `@reviewsensei/cli-linux-arm64-gnu` | `bin/review-sensei` |
| Linux glibc x64 | linux/x64 | `@reviewsensei/cli-linux-x64-gnu` | `bin/review-sensei` |
| Windows x64 | win32/x64 | `@reviewsensei/cli-win32-x64` | `bin/review-sensei.exe` |

The launcher selects only this closed table, requires a non-empty glibc report
for Linux, resolves the installed package's own metadata, checks exact version
and a regular in-package payload, and spawns with raw argv, shell disabled, and
inherited process behavior. Platform packages deliberately expose no npm `bin`
field: the launcher alone owns the `review-sensei` command and resolves the
closed-table payload path directly. It forwards supported signals and returns
the child outcome. It never downloads a runtime, runs lifecycle scripts,
contacts a provider, or falls back to system Python.

`packaging/standalone/review-sensei.spec` builds a one-folder executable from
`review_sensei.cli:main` with PyInstaller, collecting JSON defaults and
`review-sensei` distribution metadata. The pinned native builder rejects
cross-compilation and confines generated output to ignored `build/`/`dist/`
staging. The package assembler copies each matching bundle and emits a
checksum manifest. An independent validator reads only package metadata and
archive members; it rejects traversal, links/special files, private paths,
lifecycle scripts, metadata drift, and oversized archives before any package
can be published.

## Release and trust boundary

The public `malsabbagh/review-sensei` repository owns the npm source and its
manual `publish-npm.yml` release workflow. It requires a public-default-branch
dispatch, pins its full source SHA, builds the five native targets, assembles
all six packages, runs non-dry-run `npm pack`, validates those exact tarball
bytes, records SHA-256 and npm SRI, and attests the checksums. A protected,
explicitly GitHub-hosted publish job serializes each version and accepts a retry
only when every existing package has the exact attested integrity. It publishes
platform packages first and reads back exact version and `dist.integrity` for
every platform package before publishing the launcher. Each manifest names the
public repository so npm
provenance can bind the published bytes to auditable source and build
instructions.

npm cannot configure a Trusted Publisher before an initial package version
exists. The first release therefore uses a temporary, short-lived npm write
credential stored only in the protected `npm` environment and still publishes
with provenance from GitHub Actions. It is revoked after registry verification;
subsequent releases use npm OIDC Trusted Publishing bound to the exact public
repository, workflow filename, and environment. The npm lane remains separate
from PyPI publication. The existing `review-sensei-run.yml` remains PyPI-first
and executes its triggering SHA.

## Alternatives considered

1. A JavaScript/TypeScript reimplementation would duplicate provider and
   validation logic, creating behavioral drift and a larger credential
   boundary. Rejected.
2. A lifecycle downloader or system-Python fallback would make installation
   dependent on network/runtime state and undermine reproducibility. Rejected.
3. A single universal binary or cross-compiled bundle would not provide the
   native ABI guarantees required for glibc, macOS, and Windows. Rejected.

## Consequences

Consumers get a predictable `npx` command and no Python prerequisite for the
native executable. Maintainers must retain five native runner lanes, exact
version parity, public-source/package provenance, protected npm-environment
configuration, and registry readback. Native clean-machine and registry
evidence remains a maintainer gate after merge; this change does not publish
packages or reserve the npm scope.

## Partial release and rollback

The launcher is never published against a partial platform set. If a platform
publication or readback fails, stop before the launcher and preserve the
checksums, SBOM/provenance, and workflow evidence. A retry may reuse the version
only when its registry entries match the attested tarballs exactly; otherwise do
not move tags, overwrite tarballs, or infer success from a local build.
Deprecate/remove the affected recommendation and publish a higher patch version
after correcting source and workflow. A source-only revert and a new signed
version are the rollback path.

## Validation

Node built-in launcher tests cover target selection, glibc/musl rejection,
resolution, argv/shell/process inheritance, signals, exit statuses, and
sanitized failures. Python tests cover source manifests, deterministic
assembly, archive safety/size limits, lifecycle/version/license/platform
failures, and the standalone smoke-input seam. CI and release checks retain
full Action pins and the public publication-boundary audit.
