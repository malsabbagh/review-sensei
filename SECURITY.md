# Security policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private vulnerability reporting for this repository when it is enabled. If it is
not enabled, contact the maintainer privately through the
[`malsabbagh` GitHub profile](https://github.com/malsabbagh).

Include the affected version or commit, a minimal reproduction, impact, and any
safe mitigation. Remove credentials, private source code, and personal data from
the report unless they are necessary to reproduce the issue.

## Security boundaries

ReviewSensei sends review prompts and diffs to the configured provider. Users are
responsible for selecting a provider endpoint appropriate for the sensitivity of
their source code. See [data handling](docs/data-handling.md).

Lens configuration and supplemental documents must come from a reviewed target
branch or deployment bundle, never the untrusted pull-request head. Document
sources are explicit and repository-relative; the loader rejects traversal,
symlinks, secret-like files, unsupported formats, and excessive input before a
provider call.

The engine does not execute code from a diff and rejects inline locations outside
the changed lines. Future GitHub and hosted-service integrations must preserve
those boundaries and add their own authentication, replay, secret, and
untrusted-fork reviews.

## Bounded untrusted inputs and outputs

The public `ReviewLimits` profile is a downward-only security contract. Defaults
bound diffs to 1,048,576 bytes/50,000 lines/500 files/5,000 hunks, prompts to
4,194,304 bytes, provider response envelopes to 1,048,576 bytes, and results to
2,097,152 bytes. Requests additionally cap repository metadata at 512 bytes,
titles at 4,096 bytes, instructions at 65,536 bytes, model ids at 256 bytes,
metadata at 64 pairs (128-byte keys and 4,096-byte values), learnings at 100,
active categories and lens contexts at 64 each, and line numbers at
2,147,483,647. Results cap summaries at 32,768 bytes, comments at 250 with
16,384-byte bodies, and proposals at 100 (16,384 bytes each and 262,144 bytes
for the compact UTF-8 serialization of the complete proposal array, including
brackets and separators). Embedders may tighten any value but cannot raise a
public ceiling.

Canonical paths are strict NFC UTF-8 repository-relative values using `/`.
Empty/dot/parent segments, absolute or drive/UNC forms, backslashes,
surrounding whitespace, control/format/surrogate code points, and silent
normalization are rejected. Git C-quoted diff paths are decoded byte-for-byte
with only supported escapes before validation. Exact `(path, line, body)`
comment duplicates are first-wins in stable stage order; distinct bodies are
retained. Literal glob characters in real Git paths are preserved without being
interpreted. Ollama reads at most `max_response_bytes + 1` bytes before strict
UTF-8 and envelope checks. Error messages are static and do not echo prompts,
diffs, provider bodies, credentials, or secret markers.

Migration replaces non-canonical paths and context-source `path: "."` with
explicit canonical sources and splits oversized inputs. Rollback is code-only:
revert the limits/validation integration and restore the previous parser,
constructors, and provider read boundary; no stored state migration is needed.
