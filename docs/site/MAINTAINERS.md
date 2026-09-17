# Site manifest maintainers

The public site provider matrix and release facts are generated from a single
validated manifest. Keep documentation honest and release-aware by updating the
manifest whenever provider or release boundaries change.

## Files

| Path | Purpose |
| --- | --- |
| `docs/site/data/site-manifest.json` | Source of truth for provider labels and release facts |
| `docs/site/schemas/site-manifest.schema.json` | JSON Schema for the manifest |
| `scripts/validate_site_manifest.py` | Schema, evidence, version, and registry checks |
| `scripts/build_site_pages.py` | Generates `providers/index.html` and `releases/index.html` |

## Update checklist

1. Read `src/review_sensei/providers/registry.py` and `profiles.py` before
   changing provider labels. Only registered adapters may claim
   `implemented-on-main`.
2. Keep `release_facts.version` and `release_facts.tag` aligned with
   `pyproject.toml` and `packages/npm/cli/package.json`.
3. Use conservative status labels. A provider is **shipped** only when its
   registry key is registered and at least one engine surface (`cli`,
   `evaluate`, or `npx_launcher`) is enabled. Shipped providers must include
   `implemented-on-main`. Workflow-enabled shipped providers must also include
   `supported`; CLI-only shipped providers must include
   `available-in-distribution` instead. Unshipped providers (including
   source-only adapters such as
   OpenRouter today) must stay `not-implemented` and must not be labeled
   `supported`.
4. Point `evidence` entries at repository paths that exist on main.
5. Run:

   ```bash
   python scripts/validate_site_manifest.py
   python scripts/build_site_pages.py
   python -m unittest tests.test_site_manifest -v
   ```

6. Commit the manifest and regenerated HTML pages together.

CI validates the manifest and checks that committed HTML matches the manifest in
the `Schemas and Action pins` job. GitHub Pages rebuilds the static pages
after CI succeeds on `main`.
