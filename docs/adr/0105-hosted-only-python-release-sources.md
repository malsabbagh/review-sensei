# ADR 0105: Independent Python publication sources for hosted-only releases

Status: Proposed
Date: 2026-10-23
Owners/Reviewers: Maintainers
Linked issue: User-requested hosted-only release scope

## Context

`review-sensei-github` is a separate Python distribution pinned to an exact
`review-sensei` engine. npm freezes the core engine and does not wrap the hosted
package. Preparation and signing already support keeping the current engine
version and selecting only a new hosted tag. Major-channel qualification,
however, required the core and hosted tags to share the workflow source. That
coupling caused an unnecessary core/npm version for a hosted runtime repair.

The reusable workflow's source fallback builds the core from the executing
workflow commit. Simply dropping source equality would let that fallback execute
unpublished core changes under the old version. Package provenance alone does
not close that source path.

## Decision

Bind each Python publication to its own verified immutable signed tag, actual
publisher execution, workflow/ref/source, protected environment and existing
Sigstore verification. The hosted signed source equals the selected workflow
source. Core GitHub release notes, run, four assets and build attestation bind
the actual core signed source, recorded separately as `release_source_sha`.
The hosted wheel still pins the exact published engine version.

Same-source paired releases keep their existing qualification path. When core
and workflow sources differ, require exact Git type/mode/object equality for
the complete `src` tree, `pyproject.toml`, `setup.py`, `setup.cfg`, `MANIFEST.in`,
`README.md`, `LICENSE` and root `.gitattributes`, including optional-file absence.
Require complete bounded root/source inventories, regular build files and no
symlinks or gitlinks under `src`. Comparing the entire `src` tree covers newly
added sibling packages as well as the existing engine package.

Admit only the recognized current build graph: `setuptools.build_meta`, current
two build requirements, no backend path or extra build-system keys, static
project metadata, `README.md` readme, string license and `LICENSE` license-files.
The setuptools tables are exactly the current `src` discovery/package directory
and three JSON package-data patterns. `setup.cfg` is absent; `setup.py` is absent
or an AST consisting solely of an optional docstring, setuptools `setup` import
and argument-free `setup()` call. Parse these inputs as data without execution.
An identical custom backend or helper at both commits does not establish this
closed build graph and is rejected until a separately reviewed policy supports
it. This proves recognized source/build-input equivalence, not reproducible
wheel bytes: the existing build dependency range remains unchanged.

Store each project source and the equivalence proof in the PyPI receipt. Final
readback checks both immutable tag identities, all four registry artifacts and
the equivalence proof again. Legacy same-source receipts remain readable;
split-source receipts without the proof fail. Preserve the actual publisher's
`publication_attempt` independently of the newest `run_attempt`, including the
existing authenticated alias/original-execution rules.

Automatic installed qualification wakes only on successful
`Release review-sensei-github`. Both paired and hosted-only releases have that
event. Bind its exact run/attempt, source manifest, hosted tag and publisher;
resolve the core's signed source and wait read-only for its exact original
Python run. Recheck both tags and publication attempts after the wait, then run
the existing four installed CLI lanes. A paired release creates one automatic
matrix regardless of publication order. Preserve manual exact historical
qualification. No additional publisher, workflow dispatch authority, credential
or permission is introduced.

## Alternatives

- Permanently retain one-source equality: forces irrelevant core/npm publication
  and does not satisfy the requested scope.
- Resolve and pin a separate core source inside the reusable installer: valid
  broader design, but adds a runtime reference-resolution surface. Conservative
  input equivalence preserves the existing installer with a smaller change.
- Compare only the engine directory or published version: misses sibling
  packages, build configuration and external metadata inputs.
- Compare all docs/tests: unnecessarily recouples hosted documentation and sdist
  changes to the installed core wheel. The fixed recognized recipe closes the
  installed-wheel input graph while those source archives retain their own
  immutable provenance.

## Validation and rollout

Exercise hosted-only preparation with unchanged core/npm manifests and retained
prepared-receipt recovery. Test source/tag drift, cross-assigned provenance,
changed core inputs, sibling packages, attributes, symlinks/gitlinks, truncated
inventories, identical custom recipes, retained publisher attempts and both
publication orders. Keep the original signing, approval, artifact, downstream,
reader-drain and expected-old-object promotion checks.

Merge only after exact-head qualification. A later authorized hosted-only release
can keep core/npm versions and emit only the hosted tag. Existing published
versions and immutable tags remain unchanged. This contract grants no channel
promotion authority. Rollback before a new preparation can restore source
equality; a rollback after split-source activation must first account for that
qualified channel and preserve all existing immutable artifacts.

## Registry-only installation amendment

ADR 0109 adds a separately recognized registry-only core installer, introduced
in the qualified hosted 0.3.14 workflow. Its proof binds the complete reviewed
workflow bytes and the original signed core source. It makes no build-input-equivalence claim:
the new workflow never builds or executes core from its own checkout, and
fails closed when the exact published core cannot be installed. Hosted fallback
remains bound to its own signed workflow source. All publication/provenance and
installed qualification checks remain required.

Historical workflow-source fallback retains every equivalence check above.
A selector or receipt assertion is not installation authority; unknown bytes,
ambiguous proof fields or a surviving core source fallback reject the new mode.
