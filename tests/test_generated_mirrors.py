"""Tests for verified deterministic generated mirrors (configuration, verifier, coverage)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.cli import main as cli_main
from review_sensei.configuration import (
    ConfigurationError,
    GeneratedMirrorRule,
    parse_configuration_text,
    render_configuration,
    resolve_inference,
)
from review_sensei.coverage import CoverageManifest, FileCoverage, HunkCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.generated_mirrors import (
    SnapshotMirrorVerification,
    VerifiedMirrorAttestation,
    _generator_subprocess_env,
    _resolve_generator_command,
    verify_generated_mirrors,
)
from review_sensei.models import ProviderRequest, ProviderResponse, ReviewRequest
from review_sensei.outcomes import ResourceBudget
from review_sensei.providers.base import ReviewProvider
from review_sensei.service import ReviewService, review_work_policy_digest
from review_sensei.stages import ReviewCategory, Stage

GENERATOR_SCRIPT = """import json
import os
from pathlib import Path

assert "GITHUB_TOKEN" not in os.environ
assert "OPENAI_API_KEY" not in os.environ
assert os.environ.get("PYTHONHASHSEED") == "0"
assert os.environ.get("TZ") == "UTC"

src = Path("docs/operations.md").read_text(encoding="utf-8")
lines = [line.strip() for line in src.splitlines() if line.strip()]
out = Path("resources/docs/guides.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(
    json.dumps({"entries": lines}, sort_keys=True) + "\\n",
    encoding="utf-8",
    newline="\\n",
)
"""


class _RecordingProvider(ReviewProvider):
    name = "fake"
    model = "fake-model"
    allow_model_override = True

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return ProviderResponse(
            text=json.dumps(
                {
                    "summary": "Reviewed canonical changes.",
                    "comments": [],
                    "learning_proposals": [],
                }
            ),
            provider=self.name,
            model=self.model,
        )


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "CI Bot")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "core.eol", "lf")


def _commit_files(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    _git(repo, "add", *files.keys())
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _sample_rule(
    *,
    command: tuple[str, ...] | None = None,
    mirror_paths: tuple[str, ...] = ("resources/docs/guides.json",),
    timeout_seconds: int = 30,
) -> GeneratedMirrorRule:
    return GeneratedMirrorRule(
        name="docs_mirror",
        canonical_sources=("docs/operations.md",),
        mirror_paths=mirror_paths,
        generator_sources=("scripts/build_guides.py",),
        generator_command=command or (sys.executable, "scripts/build_guides.py"),
        timeout_seconds=timeout_seconds,
    )


class GeneratedMirrorConfigurationTests(unittest.TestCase):
    def test_valid_generated_mirrors_config_and_explain(self) -> None:
        text = """schema: 1
advanced:
  generated_mirrors:
    docs_mirror:
      canonical_sources:
        - docs/operations.md
      mirror_paths:
        - resources/docs/guides.json
      generator_sources:
        - scripts/build_guides.py
      generator_command:
        - python3
        - scripts/build_guides.py
      timeout_seconds: 45
"""
        cfg = parse_configuration_text(text)
        self.assertEqual(len(cfg.advanced.generated_mirrors), 1)
        rule = cfg.advanced.generated_mirrors[0]
        self.assertEqual(rule.name, "docs_mirror")
        self.assertEqual(rule.canonical_sources, ("docs/operations.md",))
        self.assertEqual(rule.mirror_paths, ("resources/docs/guides.json",))
        self.assertEqual(rule.generator_sources, ("scripts/build_guides.py",))
        self.assertEqual(rule.generator_command, ("python3", "scripts/build_guides.py"))
        self.assertEqual(rule.timeout_seconds, 45)

        resolved = resolve_inference(cfg, environ={})
        explained = render_configuration(cfg, resolved, explain=True)
        self.assertIn("generated_mirrors.docs_mirror.canonical_sources", explained)
        self.assertIn("generated_mirrors.docs_mirror.timeout_seconds: 45", explained)

        minimal = parse_configuration_text("schema: 1\n")
        minimal_explained = render_configuration(
            minimal, resolve_inference(minimal, environ={}), explain=True
        )
        self.assertIn(
            "advanced.generated_mirrors: none [packaged default]", minimal_explained
        )

    def test_rejects_invalid_generated_mirror_configs(self) -> None:
        cases = [
            (
                "shell_wrapper_sh",
                """schema: 1
advanced:
  generated_mirrors:
    docs_mirror:
      canonical_sources:
        - docs/operations.md
      mirror_paths:
        - resources/docs/guides.json
      generator_sources:
        - scripts/build_guides.py
      generator_command:
        - sh
        - -c
        - python3 scripts/build_guides.py
""",
            ),
            (
                "shell_wrapper_powershell",
                """schema: 1
advanced:
  generated_mirrors:
    docs_mirror:
      canonical_sources:
        - docs/operations.md
      mirror_paths:
        - resources/docs/guides.json
      generator_sources:
        - scripts/build_guides.py
      generator_command:
        - powershell
        - /c
        - scripts/build_guides.py
""",
            ),
            (
                "uncovered_script_token",
                """schema: 1
advanced:
  generated_mirrors:
    docs_mirror:
      canonical_sources:
        - docs/operations.md
      mirror_paths:
        - resources/docs/guides.json
      generator_sources:
        - scripts/build_guides.py
      generator_command:
        - python3
        - scripts/unlisted.py
""",
            ),
            (
                "dot_git_path",
                """schema: 1
advanced:
  generated_mirrors:
    docs_mirror:
      canonical_sources:
        - .git/config
      mirror_paths:
        - resources/docs/guides.json
      generator_sources:
        - scripts/build_guides.py
      generator_command:
        - python3
        - scripts/build_guides.py
""",
            ),
            (
                "parent_traversal_path",
                """schema: 1
advanced:
  generated_mirrors:
    docs_mirror:
      canonical_sources:
        - ../secrets.md
      mirror_paths:
        - resources/docs/guides.json
      generator_sources:
        - scripts/build_guides.py
      generator_command:
        - python3
        - scripts/build_guides.py
""",
            ),
            (
                "overlapping_mirror_and_canonical",
                """schema: 1
advanced:
  generated_mirrors:
    docs_mirror:
      canonical_sources:
        - docs/operations.md
      mirror_paths:
        - docs/operations.md
      generator_sources:
        - scripts/build_guides.py
      generator_command:
        - python3
        - scripts/build_guides.py
""",
            ),
            (
                "overlapping_mirror_across_rules",
                """schema: 1
advanced:
  generated_mirrors:
    first_mirror:
      canonical_sources:
        - docs/a.md
      mirror_paths:
        - resources/docs
      generator_sources:
        - scripts/build_guides.py
      generator_command:
        - python3
        - scripts/build_guides.py
    second_mirror:
      canonical_sources:
        - docs/b.md
      mirror_paths:
        - resources/docs/guides.json
      generator_sources:
        - scripts/build_guides.py
      generator_command:
        - python3
        - scripts/build_guides.py
""",
            ),
            (
                "timeout_out_of_range",
                """schema: 1
advanced:
  generated_mirrors:
    docs_mirror:
      canonical_sources:
        - docs/operations.md
      mirror_paths:
        - resources/docs/guides.json
      generator_sources:
        - scripts/build_guides.py
      generator_command:
        - python3
        - scripts/build_guides.py
      timeout_seconds: 121
""",
            ),
        ]
        for label, yaml_text in cases:
            with self.subTest(label=label):
                with self.assertRaises(ConfigurationError):
                    parse_configuration_text(yaml_text)

    def test_rejects_more_than_16_rules(self) -> None:
        rules_yaml = "\n".join(
            f"""    rule_{idx}:
      canonical_sources:
        - docs/src_{idx}.md
      mirror_paths:
        - resources/out_{idx}.json
      generator_sources:
        - scripts/build_{idx}.py
      generator_command:
        - python3
        - scripts/build_{idx}.py"""
            for idx in range(17)
        )
        with self.assertRaises(ConfigurationError):
            parse_configuration_text(
                f"schema: 1\nadvanced:\n  generated_mirrors:\n{rules_yaml}\n"
            )

    def test_work_policy_digest_folds_generated_mirrors(self) -> None:
        budgets = ReviewWorkBudgets(mode="unified")
        res = ResourceBudget.create()
        without = review_work_policy_digest(budgets, res)
        rule = _sample_rule()
        with_mirror = review_work_policy_digest(budgets, res, generated_mirrors=(rule,))
        self.assertIsNotNone(without)
        self.assertIsNotNone(with_mirror)
        self.assertNotEqual(without, with_mirror)

        provider = _RecordingProvider()
        svc = ReviewService(provider, work_budgets=budgets, generated_mirrors=(rule,))
        self.assertEqual(svc.work_policy_digest, with_mirror)


class GeneratedMirrorVerifierTests(unittest.TestCase):
    def _build_fixture_repo(self, root: Path) -> tuple[str, str, str]:
        _init_repo(root)
        initial_mirror = json.dumps({"entries": ["step 1"]}, sort_keys=True) + "\n"
        ancestor_sha = _commit_files(
            root,
            {
                "docs/operations.md": "step 1\n",
                "scripts/build_guides.py": GENERATOR_SCRIPT,
                "resources/docs/guides.json": initial_mirror,
            },
            "initial",
        )
        _git(root, "checkout", "-b", "feature")
        head_mirror = (
            json.dumps({"entries": ["step 1", "step 2"]}, sort_keys=True) + "\n"
        )
        head_sha = _commit_files(
            root,
            {
                "docs/operations.md": "step 1\nstep 2\n",
                "resources/docs/guides.json": head_mirror,
            },
            "update docs and mirror",
        )
        _git(root, "checkout", ancestor_sha)
        base_sha = _commit_files(
            root,
            {"README.md": "mainline update\n"},
            "advance base",
        )
        return ancestor_sha, base_sha, head_sha

    def test_three_snapshot_verification_succeeds_without_mutating_git_objects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, base_sha, head_sha = self._build_fixture_repo(repo)
            before_objects = sorted(
                p.relative_to(repo / ".git" / "objects").as_posix()
                for p in (repo / ".git" / "objects").rglob("*")
                if p.is_file()
            )
            with patch.dict(
                os.environ,
                {"GITHUB_TOKEN": "secret-gh", "OPENAI_API_KEY": "secret-oai"},
            ):
                attestations = verify_generated_mirrors(
                    repo,
                    rules=(_sample_rule(),),
                    base_sha=base_sha,
                    head_sha=head_sha,
                    policy_commit=base_sha,
                    changed_paths=("docs/operations.md", "resources/docs/guides.json"),
                )
            after_objects = sorted(
                p.relative_to(repo / ".git" / "objects").as_posix()
                for p in (repo / ".git" / "objects").rglob("*")
                if p.is_file()
            )
            self.assertEqual(before_objects, after_objects)
            self.assertEqual(len(attestations), 1)
            att = attestations[0]
            self.assertTrue(att.verified)
            self.assertIsNone(att.reason)
            self.assertEqual(
                {snap.snapshot for snap in att.snapshots}, {"base", "head", "merge"}
            )
            self.assertTrue(all(snap.verified for snap in att.snapshots))
            self.assertEqual(len(att.digest()), 64)
            self.assertEqual(att.digest(), attestations[0].digest())

    def test_tampered_mirror_at_head_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, base_sha, _ = self._build_fixture_repo(repo)
            _git(repo, "checkout", "-b", "tampered", base_sha)
            tampered_head = _commit_files(
                repo,
                {
                    "docs/operations.md": "step 1\nstep 2\n",
                    "resources/docs/guides.json": json.dumps(
                        {"entries": ["step 1", "injected-tamper"]}, sort_keys=True
                    )
                    + "\n",
                },
                "tampered mirror",
            )
            attestations = verify_generated_mirrors(
                repo,
                rules=(_sample_rule(),),
                base_sha=base_sha,
                head_sha=tampered_head,
                policy_commit=base_sha,
                changed_paths=("docs/operations.md", "resources/docs/guides.json"),
            )
            self.assertEqual(len(attestations), 1)
            self.assertFalse(attestations[0].verified)
            self.assertEqual(attestations[0].reason, "mirror-content-mismatch")

    def test_merge_snapshot_mismatch_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            base0 = _commit_files(
                repo,
                {
                    "docs/operations.md": "alpha\n",
                    "docs/extra.txt": "base0\n",
                    "scripts/build_guides.py": (
                        "import json\nfrom pathlib import Path\n"
                        "ops = Path('docs/operations.md').read_text(encoding='utf-8').strip()\n"
                        "extra = Path('docs/extra.txt').read_text(encoding='utf-8').strip()\n"
                        "out = Path('resources/docs/guides.json')\n"
                        "out.parent.mkdir(parents=True, exist_ok=True)\n"
                        "out.write_text(json.dumps({'ops': ops, 'extra': extra}) + '\\n', encoding='utf-8', newline='\\n')\n"
                    ),
                    "resources/docs/guides.json": json.dumps(
                        {"ops": "alpha", "extra": "base0"}
                    )
                    + "\n",
                },
                "base0",
            )
            _git(repo, "checkout", "-b", "pr")
            head_sha = _commit_files(
                repo,
                {
                    "docs/operations.md": "beta\n",
                    "resources/docs/guides.json": json.dumps(
                        {"ops": "beta", "extra": "base0"}
                    )
                    + "\n",
                },
                "pr changes operations",
            )
            _git(repo, "checkout", base0)
            base_sha = _commit_files(
                repo,
                {
                    "docs/extra.txt": "main-changed\n",
                    "resources/docs/guides.json": json.dumps(
                        {"ops": "alpha", "extra": "main-changed"}
                    )
                    + "\n",
                },
                "main changed extra",
            )
            # When both base and head modify guides.json differently, merge-tree hits conflict
            attestations = verify_generated_mirrors(
                repo,
                rules=(_sample_rule(),),
                base_sha=base_sha,
                head_sha=head_sha,
                policy_commit=base_sha,
                changed_paths=("docs/operations.md", "resources/docs/guides.json"),
            )
            self.assertFalse(attestations[0].verified)
            self.assertEqual(attestations[0].reason, "merge-conflict")

    def test_merge_snapshot_content_mismatch_without_conflict_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo_merge"
            _init_repo(repo)
            ancestor = _commit_files(
                repo,
                {
                    "docs/operations.md": "alpha\n",
                    "scripts/build_guides.py": (
                        "import json\nfrom pathlib import Path\n"
                        "ops = Path('docs/operations.md').read_text(encoding='utf-8').strip()\n"
                        "extra = Path('docs/extra.txt').read_text(encoding='utf-8').strip() "
                        "if (ops == 'beta' and Path('docs/extra.txt').exists()) else None\n"
                        "out = Path('resources/docs/guides.json')\n"
                        "out.parent.mkdir(parents=True, exist_ok=True)\n"
                        "out.write_text(json.dumps({'ops': ops, 'extra': extra}) + '\\n', encoding='utf-8', newline='\\n')\n"
                    ),
                    "resources/docs/guides.json": json.dumps(
                        {"ops": "alpha", "extra": None}
                    )
                    + "\n",
                },
                "ancestor",
            )
            _git(repo, "checkout", "-b", "pr", ancestor)
            pr_head = _commit_files(
                repo,
                {
                    "docs/operations.md": "beta\n",
                    "resources/docs/guides.json": json.dumps(
                        {"ops": "beta", "extra": None}
                    )
                    + "\n",
                },
                "pr changes ops",
            )
            _git(repo, "checkout", ancestor)
            main_base = _commit_files(
                repo, {"docs/extra.txt": "from-main\n"}, "main adds extra"
            )
            attestations = verify_generated_mirrors(
                repo,
                rules=(_sample_rule(),),
                base_sha=main_base,
                head_sha=pr_head,
                policy_commit=main_base,
                changed_paths=("docs/operations.md", "resources/docs/guides.json"),
            )
            self.assertFalse(attestations[0].verified)
            self.assertEqual(attestations[0].reason, "mirror-content-mismatch")
            by_snap = {s.snapshot: s for s in attestations[0].snapshots}
            self.assertTrue(by_snap["base"].verified)
            self.assertTrue(by_snap["head"].verified)
            self.assertFalse(by_snap["merge"].verified)
            self.assertEqual(by_snap["merge"].reason, "mirror-content-mismatch")

    def test_symlink_mutating_generator_and_timeout_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 1. Symlink in tree (mode 120000 created portably) -> unsupported-tree-entry
            repo1 = Path(tmp) / "repo1"
            _, base_sha, _ = self._build_fixture_repo(repo1)
            _git(repo1, "checkout", "-b", "sym", base_sha)
            sym_blob = (
                subprocess.check_output(
                    ["git", "-C", str(repo1), "hash-object", "-w", "--stdin"],
                    input=b"operations.md",
                    stderr=subprocess.PIPE,
                )
                .decode("ascii")
                .strip()
            )
            _git(
                repo1,
                "update-index",
                "--add",
                "--cacheinfo",
                f"120000,{sym_blob},docs/link.md",
            )
            _git(repo1, "commit", "-m", "add symlink")
            sym_head = _git(repo1, "rev-parse", "HEAD")
            att1 = verify_generated_mirrors(
                repo1,
                rules=(_sample_rule(),),
                base_sha=base_sha,
                head_sha=sym_head,
                policy_commit=base_sha,
                changed_paths=("docs/operations.md", "resources/docs/guides.json"),
            )
            self.assertFalse(att1[0].verified)
            self.assertEqual(att1[0].reason, "unsupported-tree-entry")

            # 2. Generator mutates canonical source -> canonical-or-generator-mutated
            repo2 = Path(tmp) / "repo2"
            _init_repo(repo2)
            mut_script = (
                "from pathlib import Path\n"
                "Path('docs/operations.md').write_text('mutated\\n', encoding='utf-8', newline='\\n')\n"
                + GENERATOR_SCRIPT
            )
            b2 = _commit_files(
                repo2,
                {
                    "docs/operations.md": "step 1\n",
                    "scripts/build_guides.py": mut_script,
                    "resources/docs/guides.json": json.dumps(
                        {"entries": ["step 1"]}, sort_keys=True
                    )
                    + "\n",
                },
                "base",
            )
            h2 = _commit_files(
                repo2,
                {
                    "docs/operations.md": "step 1\nstep 2\n",
                    "resources/docs/guides.json": json.dumps(
                        {"entries": ["step 1", "step 2"]}, sort_keys=True
                    )
                    + "\n",
                },
                "head",
            )
            att2 = verify_generated_mirrors(
                repo2,
                rules=(_sample_rule(),),
                base_sha=b2,
                head_sha=h2,
                policy_commit=b2,
                changed_paths=("docs/operations.md", "resources/docs/guides.json"),
            )
            self.assertFalse(att2[0].verified)
            self.assertEqual(att2[0].reason, "canonical-or-generator-mutated")

            # 3. Generator timeout -> generator-timeout
            repo3 = Path(tmp) / "repo3"
            _init_repo(repo3)
            slow_script = "import time\ntime.sleep(5)\n"
            b3 = _commit_files(
                repo3,
                {
                    "docs/operations.md": "step 1\n",
                    "scripts/build_guides.py": slow_script,
                    "resources/docs/guides.json": json.dumps(
                        {"entries": ["step 1"]}, sort_keys=True
                    )
                    + "\n",
                },
                "base",
            )
            h3 = _commit_files(
                repo3,
                {"docs/operations.md": "step 1\nstep 2\n"},
                "head",
            )
            att3 = verify_generated_mirrors(
                repo3,
                rules=(_sample_rule(timeout_seconds=1),),
                base_sha=b3,
                head_sha=h3,
                policy_commit=b3,
                changed_paths=("docs/operations.md", "resources/docs/guides.json"),
            )
            self.assertFalse(att3[0].verified)
            self.assertEqual(att3[0].reason, "generator-timeout")

    def test_untrusted_generator_change_refuses_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, base_sha, _ = self._build_fixture_repo(repo)
            marker = Path(tmp) / "pwned.txt"
            _git(repo, "checkout", "-b", "evil-gen", base_sha)
            evil_head = _commit_files(
                repo,
                {
                    "scripts/build_guides.py": (
                        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
                        + GENERATOR_SCRIPT
                    ),
                    "docs/operations.md": "step 1\nstep 2\n",
                    "resources/docs/guides.json": json.dumps(
                        {"entries": ["step 1", "step 2"]}, sort_keys=True
                    )
                    + "\n",
                },
                "modify generator",
            )
            attestations = verify_generated_mirrors(
                repo,
                rules=(_sample_rule(),),
                base_sha=base_sha,
                head_sha=evil_head,
                policy_commit=base_sha,
                changed_paths=(
                    "docs/operations.md",
                    "scripts/build_guides.py",
                    "resources/docs/guides.json",
                ),
            )
            self.assertFalse(marker.exists())
            self.assertEqual(len(attestations), 1)
            self.assertFalse(attestations[0].verified)
            self.assertEqual(attestations[0].reason, "untrusted-generator-change")

    def test_extra_file_in_mirror_directory_fails_set_equality(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, base_sha, _ = self._build_fixture_repo(repo)
            _git(repo, "checkout", "-b", "extra-file", base_sha)
            head_sha = _commit_files(
                repo,
                {
                    "docs/operations.md": "step 1\nstep 2\n",
                    "resources/docs/guides.json": json.dumps(
                        {"entries": ["step 1", "step 2"]}, sort_keys=True
                    )
                    + "\n",
                    "resources/docs/sneaky.json": '{"rogue": true}\n',
                },
                "extra file under mirror dir",
            )
            attestations = verify_generated_mirrors(
                repo,
                rules=(_sample_rule(mirror_paths=("resources/docs",)),),
                base_sha=base_sha,
                head_sha=head_sha,
                policy_commit=base_sha,
                changed_paths=("docs/operations.md", "resources/docs/sneaky.json"),
            )
            self.assertFalse(attestations[0].verified)
            self.assertEqual(attestations[0].reason, "mirror-file-set-mismatch")

    def test_unmatched_changed_paths_skips_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, base_sha, head_sha = self._build_fixture_repo(repo)
            attestations = verify_generated_mirrors(
                repo,
                rules=(_sample_rule(),),
                base_sha=base_sha,
                head_sha=head_sha,
                policy_commit=base_sha,
                changed_paths=("README.md",),
            )
            self.assertEqual(attestations, ())


class GeneratedMirrorCoverageAndCliTests(unittest.TestCase):
    DIFF_CANONICAL_AND_MIRROR = """diff --git a/docs/operations.md b/docs/operations.md
index 1111111..2222222 100644
--- a/docs/operations.md
+++ b/docs/operations.md
@@ -1,1 +1,2 @@
 step 1
+step 2
diff --git a/resources/docs/guides.json b/resources/docs/guides.json
index 3333333..4444444 100644
--- a/resources/docs/guides.json
+++ b/resources/docs/guides.json
@@ -1,1 +1,1 @@
-{"entries": ["step 1"]}
+{"entries": ["step 1", "step 2"]}
"""

    def _verified_attestation(self) -> VerifiedMirrorAttestation:
        snaps = tuple(
            SnapshotMirrorVerification(
                snapshot=name,
                tree_oid="a" * 40,
                verified=True,
                checked_files=("resources/docs/guides.json",),
                mirror_digests=(("resources/docs/guides.json", "b" * 64),),
            )
            for name in ("base", "head", "merge")
        )
        return VerifiedMirrorAttestation(
            rule_name="docs_mirror",
            policy_commit="1" * 40,
            base_sha="2" * 40,
            head_sha="3" * 40,
            canonical_sources=("docs/operations.md",),
            mirror_paths=("resources/docs/guides.json",),
            generator_sources=("scripts/build_guides.py",),
            snapshots=snaps,
        )

    def test_coverage_manifest_accepts_verified_generated_mirror_reason(self) -> None:
        manifest = CoverageManifest(
            files=(
                FileCoverage("docs/operations.md", "reviewed"),
                FileCoverage(
                    "resources/docs/guides.json",
                    "reviewed",
                    "verified-generated-mirror",
                ),
            ),
            hunks=(
                HunkCoverage(1, "docs/operations.md", "reviewed"),
                HunkCoverage(
                    2,
                    "resources/docs/guides.json",
                    "reviewed",
                    "verified-generated-mirror",
                ),
            ),
            enumerated_paths=("docs/operations.md", "resources/docs/guides.json"),
        )
        self.assertTrue(manifest.fully_reviewed)

    def test_request_rejects_mismatched_sha_on_verified_mirrors(self) -> None:
        att = self._verified_attestation()
        with self.assertRaises(ReviewInputError):
            ReviewRequest(
                diff=self.DIFF_CANONICAL_AND_MIRROR,
                base_sha="9" * 40,
                head_sha="3" * 40,
                verified_mirrors=(att,),
            )
        blank_att = replace(att, base_sha="")
        with self.assertRaises(ReviewInputError):
            ReviewRequest(
                diff=self.DIFF_CANONICAL_AND_MIRROR,
                base_sha="2" * 40,
                head_sha="3" * 40,
                verified_mirrors=(blank_att,),
            )

    def test_windows_command_and_env_portability_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            with (
                patch("review_sensei.generated_mirrors.os.name", "nt"),
                patch.dict(
                    os.environ,
                    {
                        "SYSTEMROOT": "C:\\Windows",
                        "COMSPEC": "C:\\Windows\\System32\\cmd.exe",
                        "PATHEXT": ".COM;.EXE;.BAT",
                        "GITHUB_TOKEN": "secret",
                    },
                    clear=True,
                ),
            ):
                env = _generator_subprocess_env(work_dir)
                self.assertEqual(env["SYSTEMROOT"], "C:\\Windows")
                self.assertEqual(env["COMSPEC"], "C:\\Windows\\System32\\cmd.exe")
                self.assertEqual(env["PATHEXT"], ".COM;.EXE;.BAT")
                self.assertNotIn("GITHUB_TOKEN", env)
                self.assertEqual(
                    _resolve_generator_command(("python3", "scripts/build_guides.py")),
                    [sys.executable, "scripts/build_guides.py"],
                )

    def test_verified_mirror_deferred_and_promoted_when_canonical_reviewed(
        self,
    ) -> None:
        att = self._verified_attestation()
        provider = _RecordingProvider()
        svc = ReviewService(
            provider,
            work_budgets=ReviewWorkBudgets(mode="unified"),
            generated_mirrors=(_sample_rule(),),
        )
        run = svc.run(
            ReviewRequest(
                diff=self.DIFF_CANONICAL_AND_MIRROR,
                verified_mirrors=(att,),
            )
        )
        self.assertEqual(run.outcome.status, "reviewed")
        self.assertEqual(len(provider.requests), 1)
        self.assertIn("diff --git a/docs/operations.md", provider.requests[0].prompt)
        self.assertNotIn(
            "diff --git a/resources/docs/guides.json", provider.requests[0].prompt
        )
        assert run.result is not None and run.result.coverage is not None
        self.assertTrue(run.result.coverage.fully_reviewed)
        by_path = {entry.path: entry for entry in run.result.coverage.files}
        self.assertEqual(by_path["docs/operations.md"].outcome, "reviewed")
        self.assertEqual(by_path["resources/docs/guides.json"].outcome, "reviewed")
        self.assertEqual(
            by_path["resources/docs/guides.json"].reason,
            "verified-generated-mirror",
        )
        mirror_hunks = [
            h
            for h in run.result.coverage.hunks
            if h.path == "resources/docs/guides.json"
        ]
        self.assertTrue(mirror_hunks)
        self.assertTrue(
            all(
                h.outcome == "reviewed" and h.reason == "verified-generated-mirror"
                for h in mirror_hunks
            )
        )

    def test_verified_mirror_stays_unreviewed_if_canonical_source_fails_or_skipped_stage(
        self,
    ) -> None:
        from review_sensei.errors import ProviderError

        att = self._verified_attestation()

        class _FailingProvider(_RecordingProvider):
            def complete(self, request: ProviderRequest) -> ProviderResponse:
                self.requests.append(request)
                raise ProviderError("provider unavailable")

        provider = _FailingProvider()
        svc = ReviewService(
            provider,
            budget=ResourceBudget.create(max_provider_calls=4),
            work_budgets=ReviewWorkBudgets(mode="unified"),
            generated_mirrors=(_sample_rule(),),
        )
        run = svc.run(
            ReviewRequest(
                diff=self.DIFF_CANONICAL_AND_MIRROR,
                verified_mirrors=(att,),
            )
        )
        self.assertEqual(run.outcome.status, "partial")
        assert run.result is not None and run.result.coverage is not None
        self.assertFalse(run.result.coverage.fully_reviewed)
        by_path = {entry.path: entry for entry in run.result.coverage.files}
        self.assertEqual(by_path["docs/operations.md"].outcome, "budget-exhausted")
        self.assertEqual(by_path["resources/docs/guides.json"].outcome, "unsupported")
        self.assertEqual(
            by_path["resources/docs/guides.json"].reason, "cross-file-relationship"
        )

        # Applicable stage skipped (no active categories for it) -> fail closed
        cat = ReviewCategory("cat_a", "Cat A", ("check",), applies_to=("docs/**",))
        stage = Stage(
            "only_docs",
            "<diff>{diff}</diff>\n{review_categories}",
            ("summary", "comments"),
            categories=(cat,),
        )
        svc2 = ReviewService(
            _RecordingProvider(),
            stages=(stage,),
            work_budgets=ReviewWorkBudgets(mode="unified"),
            generated_mirrors=(_sample_rule(),),
        )
        run2 = svc2.run(
            ReviewRequest(
                diff=self.DIFF_CANONICAL_AND_MIRROR,
                active_category_ids=(),
                verified_mirrors=(att,),
            )
        )
        assert run2.result is not None and run2.result.coverage is not None
        self.assertFalse(run2.result.coverage.fully_reviewed)
        by_path2 = {entry.path: entry for entry in run2.result.coverage.files}
        self.assertEqual(by_path2["resources/docs/guides.json"].outcome, "unsupported")

    def test_unverified_mirror_falls_back_to_normal_review(self) -> None:
        unverified = replace(
            self._verified_attestation(), snapshots=(), reason="mirror-content-mismatch"
        )
        provider = _RecordingProvider()
        svc = ReviewService(
            provider,
            work_budgets=ReviewWorkBudgets(mode="unified"),
            generated_mirrors=(_sample_rule(),),
        )
        run = svc.run(
            ReviewRequest(
                diff=self.DIFF_CANONICAL_AND_MIRROR,
                verified_mirrors=(unverified,),
            )
        )
        self.assertEqual(len(provider.requests), 1)
        self.assertIn("resources/docs/guides.json", provider.requests[0].prompt)
        assert run.result is not None and run.result.coverage is not None
        by_path = {entry.path: entry for entry in run.result.coverage.files}
        self.assertIsNone(by_path["resources/docs/guides.json"].reason)

    def test_cli_wires_generated_mirror_verification_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            cfg_text = """schema: 1
inference:
  backend: local-ollama
  model: qwen3.5:4b
advanced:
  review_work:
    mode: unified
  generated_mirrors:
    docs_mirror:
      canonical_sources:
        - docs/operations.md
      mirror_paths:
        - resources/docs/guides.json
      generator_sources:
        - scripts/build_guides.py
      generator_command:
        - python3
        - scripts/build_guides.py
"""
            base_sha = _commit_files(
                repo,
                {
                    ".reviewsensei.yml": cfg_text,
                    "docs/operations.md": "step 1\n",
                    "scripts/build_guides.py": GENERATOR_SCRIPT,
                    "resources/docs/guides.json": json.dumps(
                        {"entries": ["step 1"]}, sort_keys=True
                    )
                    + "\n",
                },
                "base",
            )
            head_sha = _commit_files(
                repo,
                {
                    "docs/operations.md": "step 1\nstep 2\n",
                    "resources/docs/guides.json": json.dumps(
                        {"entries": ["step 1", "step 2"]}, sort_keys=True
                    )
                    + "\n",
                },
                "head",
            )
            diff_text = subprocess.check_output(
                ["git", "-C", str(repo), "diff", base_sha, head_sha],
                text=True,
                encoding="utf-8",
            )
            diff_file = Path(tmp) / "pr.diff"
            diff_file.write_text(diff_text, encoding="utf-8", newline="\n")
            fixture_resp = Path(tmp) / "resp.json"
            fixture_resp.write_text(
                json.dumps(
                    {
                        "summary": "Canonical doc checked.",
                        "comments": [],
                        "learning_proposals": [],
                    }
                ),
                encoding="utf-8",
                newline="\n",
            )
            out_file = Path(tmp) / "result.json"
            code = cli_main(
                [
                    "--provider",
                    "fixture",
                    "--fixture-response",
                    str(fixture_resp),
                    "--config",
                    str(repo / ".reviewsensei.yml"),
                    "--context-root",
                    str(repo),
                    "--base-sha",
                    base_sha,
                    "--head-sha",
                    head_sha,
                    "--diff",
                    str(diff_file),
                    "--output",
                    str(out_file),
                    "--format",
                    "json",
                ]
            )
            self.assertEqual(code, 0)
            payload = json.loads(out_file.read_text(encoding="utf-8"))
            files = {item["path"]: item for item in payload["coverage"]["files"]}
            self.assertEqual(files["docs/operations.md"]["outcome"], "reviewed")
            self.assertEqual(files["resources/docs/guides.json"]["outcome"], "reviewed")
            self.assertEqual(
                files["resources/docs/guides.json"]["reason"],
                "verified-generated-mirror",
            )


if __name__ == "__main__":
    unittest.main()
