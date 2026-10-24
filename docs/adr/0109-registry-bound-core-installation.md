# ADR 0109: Registry-bound core installation

Status: Proposed. Implemented in the qualified hosted 0.3.14 workflow.
Channel activation and installed-version verification remain separate gates.

## Problem

The reusable workflow's source fallback builds core from its executing commit.
ADR 0105 therefore requires exact core build-input equality when reusing a
published core version. A later documentation change correctly marks shipped
functionality available, but changes root README package metadata. The existing
gate must reject that mismatch; excluding README alone would build unpublished
metadata under an immutable version.

## Decision

All four new runtime installers require the exact core distribution from PyPI.
Core installation failures and installed-version mismatches stop the job. No
Git fallback, alternate version or credential/permission expansion is allowed.
Hosted installation keeps its own signed-source fallback and exact core pin.
This deliberately trades core fallback availability for a smaller trust surface.

The selector `reviewsensei-core-installation: pypi-exact-v1` requests validation;
it grants no authority. Trusted validator policy contains an append-only list
of SHA-256 digests of complete reviewed registry-only workflows. Each digest
requires review of all workflow execution paths, not merely installer fragments.
Any byte change needs a reviewed allowlist addition. Never learn these digests
from selected release data, a marker, or a receipt. Retain historical digests.

Read the workflow at the exact selected commit using bounded complete Git tree
inventories, require a regular file, verify the Git blob identity, and hash its
exact bytes without normalization. A distinct installation proof binds the
engine source, hosted/workflow source, workflow path, blob and SHA-256 identity,
and all four installation lanes. Re-read this proof at final validation. Missing,
forged, mixed or unrecognized proof fails; failed recognition never falls back
to build-input equivalence.

Both immutable signed tags, original publication attempts, artifact digests,
Sigstore and GitHub core release assets, exact hosted core dependency, installed
consumer lanes and final tag/registry readbacks remain mandatory. This is not a
general proof that arbitrary workflow code cannot execute core source: only the
fully reviewed byte sequences are admitted.

## Alternatives and compatibility

Building core from its original signed source is also sound, but requires an
authenticated version/tag-object/commit binding propagated through bootstrap and
every installer. A floating version-tag URL alone is insufficient. Registry-only
installation is the narrower repair. Reverting current instructions to falsely
describe shipped functionality as unreleased is not an acceptable workaround.

Historical workflows without the new selector keep their existing source
equivalence requirements and receipt readback. Do not rewrite immutable releases
or claim a historical workflow used the new contract. Rollback to a historical
workflow needs its original qualified source/proof, not just a selector change.

## Validation

Execute all four actual installer scripts with credential-free fake transports:
missing core, network/auth failure and wrong installed version fail without any
core Git installation; exact registry pairs and hosted-only fallback succeed.
Test altered workflow bytes, a single surviving core fallback, duplicate/unknown
selectors, malformed/truncated/symlink inventories and tampered Git blobs. Test
missing, forged, mixed and source-mismatched receipts alongside historical
same-source/equivalence behavior. Retain all cryptographic publication checks
and clean installed actual core wheel plus new hosted wheel qualification.
