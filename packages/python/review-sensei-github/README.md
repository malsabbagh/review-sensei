# review-sensei-github

GitHub Actions runtime for [ReviewSensei](https://github.com/malsabbagh/review-sensei).

This distribution is the runner side of the hosted review: the OIDC broker
client, the session ledger, and GitHub publication. It is not the local review
engine. Install `review-sensei` for a local review against an LLM provider.

The reusable workflow installs the exact version this repository declares in
`packages/python/review-sensei-github/pyproject.toml`. That version depends on
one exact `review-sensei` release.
