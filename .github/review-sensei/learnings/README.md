# Repository learnings

This directory contains maintainer-approved ReviewSensei context. Store one JSON
object per learning, and merge changes through ordinary repository review.

Each entry should include:

- `id`: lowercase stable identifier;
- `title`: short name;
- `rule`: precise detail future reviews should remember;
- `scope`: repository-relative paths or glob patterns, or `["*"]` for global
  context;
- optional `rationale`, `category`, and `source`.

Use `"status": "retired"` to keep an entry's history without sending it to
future review prompts. Do not store credentials, private keys, raw diffs, or
secrets here. A learning proposal is not trusted until a maintainer reviews and
merges its PR.
