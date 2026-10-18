# Unpublished native/npm candidate qualification

This tier covers candidate artifacts from the exact checked-out CI commit. It
adds deterministic evidence for #246/#117/#146; it does not publish packages,
qualify previously published bytes, perform a registry installation, or rehearse
release rollout/rollback.

CI reuses its existing Debian 12 Linux arm64/x64 builds and Intel macOS build.
The Intel job retains Python **3.11.9**, static OpenSSL installation, frozen crypto
inspection and native parity. Only the missing ARM macOS and Windows x64 native
builds are added. A separate release-style five-build workflow would repeat the
three existing builds, so that placement was rejected.

The five build outputs are archived in permission-preserving tarballs. Safe
internal PyInstaller framework symlinks are materialized into regular candidate
files, as required by the existing npm package contract; escaping/cyclic links
refuse. The receipt records every materialized link and the normalization version.
Original executable bytes remain identical. Each
native receipt identifies the exact source commit, engine version, target,
archive SHA-256, executable SHA-256 and every bundled runtime file's length and
SHA-256. The source worktree must be clean. The npm assembly job requires all
five matching receipts, restores and verifies complete inventories, reuses
`prepare_npm_packages.py` and `validate_npm_package.py`, then runs real
`npm pack --ignore-scripts` for the launcher and all five platform packages.
The packed receipt binds all six exact tarball SHA-256 values to the same source
and native inventories.

Five matching-platform consumer jobs download that exact packed artifact. Each
uses an empty temporary npm cache, empty user/global npm configuration,
`--offline --ignore-scripts --omit=optional`, explicit local launcher/platform
file dependencies and a non-routable registry. Optional registry fallback is
excluded. Installation occurs outside the checkout in a temporary directory.
Every installed native runtime file is compared with its original build receipt.
The installed native executable runs the existing strict `standalone_smoke.py`
parity suite; the actual installed Node launcher runs version, help and a
synthetic required-fix review with the documented exit code. No provider call or
credential is needed.

Receipts and tarballs are retained as CI artifacts for seven days:

- `candidate-native-<target>` contains the tarball and native receipt.
- `candidate-npm-tarballs` contains the six exact npm tarballs and combined receipt.
- `candidate-consumer-<target>` identifies the source, selected tarball hashes,
  installed executable hash, tested cases and explicit candidate-only boundary.

The new build/assembly/consumer jobs are mandatory dependencies of the existing
`Required checks` aggregate. Every prior dependency remains required. Matrix
fail-fast is disabled; build and consumer jobs have bounded timeouts. Only public
pip downloads use setup-python caching with pinned requirements and package
metadata as inputs; native binaries and final npm artifacts are never cached
across source commits. Existing superseded-PR cancellation remains in place.

`tests/test_candidate_artifacts.py` exercises actual archive round trips and real
npm packing of synthetic non-executable payloads, dirty/wrong source refusal,
missing targets, changed archive/tarball bytes, incorrect complete inventories,
unsafe tar entries and mandatory workflow placement. Those synthetic fixtures
prove integrity/orchestration contracts, not native execution. The macOS arm64
host can additionally build and execute a real native candidate locally. Linux,
Intel macOS and Windows execution evidence must come from their corresponding CI
jobs; adding the jobs does not itself establish a passing run.

This is a reversible test/CI extension of existing native/npm build contracts.
It changes no public API, package format, release workflow, permissions, secrets,
deployment topology or ownership, so no ADR is required. Native GitHub-hosted
operation compatibility, paid/live provider quality, external clean-host registry
installation and release migration/rollback remain separately qualified scopes.

## Local ARM macOS evidence (2026-10-18)

The actual PyInstaller 6.16.0 ARM bundle passed all ten existing standalone parity
cases. After materializing its four internal Python.framework links, its exact
local npm platform tarball and launcher tarball were installed together offline
with scripts and optional dependencies omitted. The installed native bytes
matched the receipt, repeated all ten parity cases, and the installed Node
launcher passed version/help/required-fix exit semantics.

This scoped receipt identifies `a5899a53b58a9a4da4f4afca1330ece580b1f314`, Python
3.14.6 and Node 24.18.0 on ARM macOS. CI intentionally tests its own fresh source
artifacts with Python 3.11 and Node 22; the local results do not substitute for
those runs or the other four platforms.

| Local candidate artifact | SHA-256 |
| --- | --- |
| Native tarball | `f4400cf25026471f1d259c2fdcaa1105478f8afed54d126b560ec0d43e4849e3` |
| Built and installed native executable | `12fda3f3e1d98d271d73e947ab6dfbd28cfc3e9ad7b51013ac233ba9517769b3` |
| Launcher npm tarball | `510360146d7bbf814b24fe211622ae55e0d34d68779149c0b2a028b9812340b1` |
| ARM macOS npm tarball | `1e1b31033e404cbb70d3bedeecc4cf0ed0f0c670ac55a39f3d419a46eb376792` |
