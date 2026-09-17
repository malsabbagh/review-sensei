#!/usr/bin/env python3
"""Generate static provider and release pages from the site manifest."""

from __future__ import annotations

import argparse
import html
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "site" / "data" / "site-manifest.json"
SITE_ROOT = ROOT / "docs" / "site"
PROVIDERS_PAGE = SITE_ROOT / "providers" / "index.html"
RELEASES_PAGE = SITE_ROOT / "releases" / "index.html"

STATUS_COLORS = {
    "supported": ("var(--jade-bright)", "rgba(85,184,155,.08)"),
    "implemented-on-main": ("var(--jade-bright)", "rgba(85,184,155,.08)"),
    "available-in-distribution": ("#e0b665", "rgba(217,162,77,.08)"),
    "experimental": ("#e0b665", "rgba(217,162,77,.08)"),
    "planned": ("var(--red-soft)", "rgba(225,90,66,.08)"),
    "source-only": ("var(--red-soft)", "rgba(225,90,66,.08)"),
    "not-implemented": ("#ee7589", "rgba(223,95,115,.08)"),
}


def _load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("site manifest must be a JSON object")
    return value


def _site_styles() -> str:
    return """
:root{
  --ink:#090a0c;--paper:#f2efe7;--muted:#8f928f;--line:#262a2f;
  --red:#e15a42;--red-soft:#f07a63;--jade:#55b89b;--jade-bright:#74cdb1;
  --amber:#d9a24d;--radius:18px;--shadow:0 24px 70px rgba(0,0,0,.34);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;color:var(--paper);
  background:linear-gradient(180deg,#08090b 0%,#0a0b0e 48%,#090a0c 100%);
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  letter-spacing:-.01em;
}
body:before{
  content:"";position:fixed;inset:0;z-index:-3;pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,.018) 1px,transparent 1px);
  background-size:64px 64px;
  mask-image:linear-gradient(to bottom,rgba(0,0,0,.45),transparent 86%);
}
body:after{
  content:"";position:fixed;left:0;top:0;bottom:0;width:3px;z-index:80;
  background:linear-gradient(180deg,var(--red),rgba(225,90,66,.15),transparent 85%);
}
a{color:inherit;text-decoration:none}
.container{width:min(1180px,calc(100% - 36px));margin-inline:auto}
.skip-link{
  position:absolute;left:-9999px;top:0;z-index:100;padding:10px 16px;
  background:var(--red);color:#0b0c0e;font-weight:700;border-radius:0 0 8px 0;
}
.skip-link:focus{left:0}
.nav{
  position:sticky;top:0;z-index:50;backdrop-filter:blur(16px);
  background:rgba(9,10,12,.82);border-bottom:1px solid rgba(255,255,255,.055);
}
.nav-inner{display:flex;align-items:center;justify-content:space-between;height:72px}
.brand{display:flex;align-items:center;gap:12px;font-weight:760;letter-spacing:-.03em}
.dev{color:var(--red)}
.nav-links{display:flex;align-items:center;gap:26px;color:#a5aaa7;font-size:14px}
.nav-links a:hover{color:#fff}
.nav-links a[aria-current="page"]{color:#fff;font-weight:700}
.section{padding:72px 0 92px}
.section-head{max-width:760px;margin-bottom:36px}
.kicker{
  color:var(--red-soft);font-size:12px;text-transform:uppercase;
  letter-spacing:.18em;font-weight:850;
}
h1{font-size:clamp(40px,5vw,64px);line-height:1.02;letter-spacing:-.05em;margin:12px 0 14px}
h2{font-size:clamp(28px,3vw,40px);line-height:1.08;letter-spacing:-.04em;margin:0 0 12px}
.section-head p,.lede{color:#969b96;font-size:17px;line-height:1.7;margin:0}
.grid{display:grid;gap:14px}
.provider-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
.card{
  padding:22px;border-radius:16px;background:#0f1114;border:1px solid #24292f;
}
.card h3{margin:0 0 8px;font-size:18px;letter-spacing:-.02em}
.card p,.card li,.card dd{color:#858a85;line-height:1.65;font-size:14px}
.meta-list{margin:14px 0 0;padding:0;list-style:none;display:grid;gap:8px}
.meta-list li{display:grid;grid-template-columns:140px 1fr;gap:10px}
.meta-list strong{color:#dedbd2;font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.label-row{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 0}
.label{
  font-size:10px;padding:4px 8px;border-radius:999px;border:1px solid #2b2f35;
  background:#13161a;color:#b4b0a6;
}
.label.positive{color:var(--jade-bright);border-color:rgba(85,184,155,.22);background:rgba(85,184,155,.05)}
.label.warn{color:#efc56f;border-color:rgba(217,162,77,.22);background:rgba(217,162,77,.05)}
.label.muted{color:#ee7589;border-color:rgba(223,95,115,.22);background:rgba(223,95,115,.05)}
.evidence{margin-top:14px}
.evidence code,.path-chip{
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:11px;color:#b7bbb6;
}
.path-chip{display:inline-block;padding:4px 7px;border-radius:7px;background:#07090b;border:1px solid #252a30;margin:4px 6px 0 0}
.release-panel{
  padding:28px;border-radius:18px;border:1px solid #272c32;background:#0d0f12;
  box-shadow:0 22px 60px rgba(0,0,0,.22);max-width:860px;
}
.release-panel dl{display:grid;grid-template-columns:180px 1fr;gap:10px 16px;margin:0}
.release-panel dt{color:#dedbd2;font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.release-panel dd{margin:0;color:#a6aba7;font-size:14px;line-height:1.6}
.footer{border-top:1px solid #24282d;padding:28px 0 40px;color:#70756f;font-size:12px}
.footer-inner{display:flex;justify-content:space-between;gap:20px;align-items:center;flex-wrap:wrap}
.footer-links{display:flex;gap:18px;flex-wrap:wrap}
@media (max-width:980px){.provider-grid{grid-template-columns:1fr}}
@media (max-width:640px){
  .container{width:min(100% - 24px,1180px)}
  .meta-list li{grid-template-columns:1fr}
  .release-panel dl{grid-template-columns:1fr}
}
"""


def _label_class(label: str) -> str:
    if label in {"supported", "implemented-on-main", "available-in-distribution"}:
        return "positive"
    if label in {"experimental", "planned", "source-only"}:
        return "warn"
    return "muted"


def _render_labels(labels: list[str]) -> str:
    parts = []
    for label in labels:
        css = _label_class(label)
        parts.append(
            f'<span class="label {css}">{html.escape(label.replace("-", " "))}</span>'
        )
    return "".join(parts)


def _render_support_row(name: str, enabled: bool) -> str:
    state = "yes" if enabled else "no"
    return f"<li><strong>{html.escape(name)}</strong><span>{state}</span></li>"


def _page_shell(
    *,
    title: str,
    description: str,
    canonical_path: str,
    current_nav: str,
    body: str,
) -> str:
    nav_items = [
        ("home", "/", "Home"),
        ("providers", "/providers/", "Providers"),
        ("releases", "/releases/", "Releases"),
    ]
    nav_links = []
    for key, href, label in nav_items:
        current = ' aria-current="page"' if key == current_nav else ""
        nav_links.append(f'<a href="{href}"{current}>{label}</a>')
    nav_html = "\n      ".join(nav_links)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="theme-color" content="#070913" />
<link rel="icon" type="image/svg+xml" href="/assets/favicon.svg" />
<link rel="canonical" href="https://reviewsensei.dev{canonical_path}" />
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}" />
<style>{_site_styles()}</style>
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
<header class="nav">
  <div class="container nav-inner">
    <a class="brand" href="/" aria-label="ReviewSensei home">
      <span>ReviewSensei<span class="dev">.dev</span></span>
    </a>
    <nav class="nav-links" aria-label="Primary">
      {nav_html}
    </nav>
  </div>
</header>
<main id="main">
{body}
</main>
<footer class="footer">
  <div class="container footer-inner">
    <div class="brand" style="font-size:13px"><span>ReviewSensei<span class="dev">.dev</span></span></div>
    <div class="footer-links">
      <a href="https://github.com/malsabbagh/review-sensei">GitHub</a>
      <a href="https://github.com/malsabbagh/review-sensei/blob/main/docs/site/MAINTAINERS.md">Site maintainers</a>
      <a href="https://github.com/malsabbagh/review-sensei/blob/main/docs/installation.md">Docs</a>
    </div>
  </div>
</footer>
</body>
</html>
"""


def render_providers_page(manifest: dict[str, Any]) -> str:
    cards = []
    for entry in manifest["providers"]:
        profile = entry.get("profile")
        profile_text = profile if isinstance(profile, str) else "none"
        credential = entry.get("credential_ref")
        credential_text = credential if isinstance(credential, str) else "none"
        destination = entry["inference_destination"]
        model = destination.get("default_model")
        model_text = model if isinstance(model, str) else "not fixed"
        workflow = entry["workflow_support"]
        provider_mode = workflow.get("provider_mode")
        provider_mode_text = (
            provider_mode if isinstance(provider_mode, str) else "not applicable"
        )
        evidence = "".join(
            f'<code class="path-chip">{html.escape(path)}</code>'
            for path in entry["evidence"]
        )
        limitations = "".join(
            f"<li>{html.escape(item)}</li>" for item in entry.get("limitations", ())
        )
        cards.append(
            f"""
<article class="card">
  <h3>{html.escape(entry["title"])}</h3>
  <div class="label-row">{_render_labels(entry["status_labels"])}</div>
  <ul class="meta-list">
    <li><strong>Provider</strong><span>{html.escape(entry["provider"])}</span></li>
    <li><strong>Profile</strong><span>{html.escape(profile_text)}</span></li>
    <li><strong>Destination</strong><span>{html.escape(destination["base_url"])} ({html.escape(destination["endpoint_scope"])})</span></li>
    <li><strong>Model</strong><span>{html.escape(model_text)}</span></li>
    <li><strong>Credential</strong><span>{html.escape(credential_text)}</span></li>
    <li><strong>Workflow</strong><span>{html.escape(provider_mode_text)} · reusable workflow {"yes" if workflow["reusable_workflow"] else "no"}</span></li>
  </ul>
  <p>{html.escape(workflow["notes"])}</p>
  <ul class="meta-list">
    {_render_support_row("CLI", entry["engine_support"]["cli"])}
    {_render_support_row("evaluate", entry["engine_support"]["evaluate"])}
    {_render_support_row("npx launcher", entry["engine_support"]["npx_launcher"])}
  </ul>
  <div class="evidence"><strong style="color:#dedbd2;font-size:12px;text-transform:uppercase;letter-spacing:.08em">Evidence</strong><div>{evidence}</div></div>
  <ul style="margin-top:12px;padding-left:18px">{limitations}</ul>
</article>"""
        )
    body = f"""
<section class="section">
  <div class="container">
    <div class="section-head">
      <div class="kicker">Provider matrix</div>
      <h1>Release-aware provider support</h1>
      <p class="lede">Conservative labels derived from the shipped registry, named profiles, and installed workflow contract. Planned adapters stay explicitly not implemented.</p>
    </div>
    <div class="grid provider-grid">
      {"".join(cards)}
    </div>
  </div>
</section>"""
    return _page_shell(
        title="ReviewSensei providers",
        description="Conservative provider matrix for Ollama local, Ollama Cloud, OpenAI-compatible, and planned OpenRouter support.",
        canonical_path="/providers/",
        current_nav="providers",
        body=body,
    )


def render_releases_page(manifest: dict[str, Any]) -> str:
    release = manifest["release_facts"]
    workflow_rows = []
    for reference in release["workflow_references"]:
        ref = reference.get("ref")
        ref_text = f"@{ref}" if isinstance(ref, str) else ""
        notes = reference.get("notes")
        note_html = (
            f"<div style='margin-top:4px;color:#777d78'>{html.escape(notes)}</div>"
            if isinstance(notes, str)
            else ""
        )
        workflow_rows.append(
            "<li>"
            f"<strong>{html.escape(reference['name'])}</strong>"
            f"<span><code class='path-chip'>{html.escape(reference['path'])}</code>"
            f"{html.escape(ref_text)}{note_html}</span>"
            "</li>"
        )
    platform_packages = release.get("npm_platform_packages", ())
    platform_html = ", ".join(html.escape(name) for name in platform_packages)
    docs = release.get("documentation", ())
    docs_html = "".join(
        f'<code class="path-chip">{html.escape(path)}</code>' for path in docs
    )
    compatibility_manifest = release.get("compatibility_manifest")
    compatibility_row = ""
    if isinstance(compatibility_manifest, str) and compatibility_manifest:
        compatibility_row = (
            "<dt>Compatibility manifest</dt>"
            f'<dd><code class="path-chip">{html.escape(compatibility_manifest)}</code></dd>'
        )
    body = f"""
<section class="section">
  <div class="container">
    <div class="section-head">
      <div class="kicker">Release facts</div>
      <h1>Version {html.escape(release["version"])}</h1>
      <p class="lede">Manifest release facts stay aligned with pyproject.toml and the npm launcher package. Last updated {html.escape(manifest["last_updated"])}.</p>
    </div>
    <div class="release-panel">
      <dl>
        <dt>Version</dt><dd>{html.escape(release["version"])}</dd>
        <dt>Tag</dt><dd>{html.escape(release["tag"])}</dd>
        <dt>Python package</dt><dd>{html.escape(release["python_package"])}</dd>
        <dt>npm launcher</dt><dd>{html.escape(release["npm_launcher"])}</dd>
        <dt>Platform packages</dt><dd>{platform_html}</dd>
        {compatibility_row}
      </dl>
      <h2 style="margin-top:28px">Workflow references</h2>
      <ul class="meta-list">{"".join(workflow_rows)}</ul>
      <h2 style="margin-top:28px">Documentation</h2>
      <div>{docs_html}</div>
    </div>
  </div>
</section>"""
    return _page_shell(
        title="ReviewSensei releases",
        description="Release-aware facts for ReviewSensei 0.1.1 packages, workflows, and compatibility manifest references.",
        canonical_path="/releases/",
        current_nav="releases",
        body=body,
    )


def build_site_pages(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    providers_output: Path = PROVIDERS_PAGE,
    releases_output: Path = RELEASES_PAGE,
) -> tuple[Path, Path]:
    manifest = _load_manifest(manifest_path)
    providers_output.parent.mkdir(parents=True, exist_ok=True)
    releases_output.parent.mkdir(parents=True, exist_ok=True)
    providers_output.write_text(render_providers_page(manifest), encoding="utf-8")
    releases_output.write_text(render_releases_page(manifest), encoding="utf-8")
    return providers_output, releases_output


def check_site_pages(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    providers_output: Path = PROVIDERS_PAGE,
    releases_output: Path = RELEASES_PAGE,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        generated_providers = directory / "providers" / "index.html"
        generated_releases = directory / "releases" / "index.html"
        build_site_pages(
            manifest_path=manifest_path,
            providers_output=generated_providers,
            releases_output=generated_releases,
        )
        for committed, generated in (
            (providers_output, generated_providers),
            (releases_output, generated_releases),
        ):
            if committed.read_text(encoding="utf-8") != generated.read_text(
                encoding="utf-8"
            ):
                raise ValueError(f"committed site page is stale: {committed}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--providers-output", type=Path, default=PROVIDERS_PAGE)
    parser.add_argument("--releases-output", type=Path, default=RELEASES_PAGE)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed pages match the manifest without writing files",
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            check_site_pages(
                manifest_path=args.manifest,
                providers_output=args.providers_output,
                releases_output=args.releases_output,
            )
            print("site pages are up to date")
            return 0
        providers, releases = build_site_pages(
            manifest_path=args.manifest,
            providers_output=args.providers_output,
            releases_output=args.releases_output,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"site page generation failed: {exc}", file=sys.stderr)
        return 1
    print(f"generated {providers}")
    print(f"generated {releases}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
