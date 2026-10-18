# Validation evidence

Receipts in this directory are immutable observations of the named version and
wheel digests, not a rolling claim about the latest package. A newer release
does not invalidate an older failure receipt. Do not replace historical hashes
or reinterpret local candidate bytes as published wheels with the same version.

- [Expanded current candidate receipt](remaining-lifecycle-candidate.json) and
  [published 0.8.1 comparison](published-0.8.1-expanded-lifecycle.json): exact
  artifact digests, current test inventory and preserved new acceptance failures.
- [Remaining lifecycle acceptance and issue map](remaining-lifecycle.md): current
  supported paths, exact test seams, candidate/published identity separation
  and unresolved installation/production qualification.
- [Approval lifecycle matrix and original CI measurements](approval-lifecycle.md):
  initial PR309 work based on the PR308 merge. Its 0.7.1/0.2.1 local candidate
  builds and PyPI observations are historical.
- [Published 0.7.1/0.2.1](published-0.7.1-lifecycle.json): historical failure receipt
  from the initial lifecycle suite snapshot. The later 27-method suite also
  failed against those same old wheels; see PR309's historical description.
- [Published 0.8.0/0.3.0](published-0.8.0-lifecycle.json): all six suites passed
  1,109 rows, including 27 lifecycle methods, in a clean macOS arm64/Python 3.14
  environment with exact PyPI wheels, verified origins and no source fallback.
- [PR309 review dispositions](pr309-review-dispositions.md): findings at head
  `b04230946ce3b2f13707ed0cc74780701ac869c1`, code/test responses and limits.

Run `scripts/qualify_published_cli.py` with exact engine/adapter versions and a new
output directory to create a fresh receipt. Default scope includes all six
suites. Each report lists selected/excluded suites; a subset does not establish
full qualification. Large raw reports stay in local or Actions artifacts, not in
this directory. PR309's current description links the latest candidate CI and
actual hosted review separately from these published-wheel snapshots.
