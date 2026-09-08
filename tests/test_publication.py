from __future__ import annotations

import importlib
import importlib.util
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_publish_script():
    """Load publish_public.py with scripts/ on sys.path for its import."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return load_script("publish_public.py")


BOUNDARY = load_script("validate_publication_boundary.py")
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
AUDIT = load_script("audit_publication.py")


def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo with an initial commit."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


def _write_exclusions(path: Path, entries: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


class ExclusionManifestTests(unittest.TestCase):
    def test_load_valid_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            excl_path = tmp_path / "exclusions.json"
            _write_exclusions(
                excl_path,
                [
                    {"pattern": ".env", "reason": "secrets"},
                ],
            )
            result = BOUNDARY.load_exclusions(excl_path)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["pattern"], ".env")

    def test_load_missing_manifest_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BOUNDARY.PublicationBoundaryError):
                BOUNDARY.load_exclusions(Path(tmp) / "nonexistent.json")

    def test_load_non_array_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            excl_path = Path(tmp) / "exclusions.json"
            excl_path.write_text('{"not": "an array"}', encoding="utf-8")
            with self.assertRaises(BOUNDARY.PublicationBoundaryError):
                BOUNDARY.load_exclusions(excl_path)

    def test_load_missing_reason_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            excl_path = Path(tmp) / "exclusions.json"
            excl_path.write_text('[{"pattern": ".env"}]', encoding="utf-8")
            with self.assertRaises(BOUNDARY.PublicationBoundaryError):
                BOUNDARY.load_exclusions(excl_path)


class BoundaryValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        _init_git_repo(self.tmp_path)
        self.excl_path = self.tmp_path / ".publication" / "exclusions.json"
        _write_exclusions(
            self.excl_path,
            [
                {"pattern": ".internal/**", "reason": "internal only"},
                {"pattern": ".env", "reason": "secrets"},
            ],
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_publishable_excludes_patterns(self):
        (self.tmp_path / ".internal").mkdir()
        (self.tmp_path / ".internal" / "secret.md").write_text("x", encoding="utf-8")
        (self.tmp_path / "src.py").write_text("print(1)\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add files"],
            cwd=self.tmp_path,
            check=True,
        )
        publishable, excluded = BOUNDARY.validate_boundary(
            self.tmp_path, self.excl_path
        )
        self.assertIn("src.py", publishable)
        self.assertIn(".internal/secret.md", excluded)
        self.assertNotIn(".internal/secret.md", publishable)

    def test_secret_path_detected(self):
        (self.tmp_path / "id_rsa").write_text("not a real key", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add key file"],
            cwd=self.tmp_path,
            check=True,
        )
        with self.assertRaises(BOUNDARY.PublicationBoundaryError) as ctx:
            BOUNDARY.validate_boundary(self.tmp_path, self.excl_path)
        self.assertIn("id_rsa", str(ctx.exception))

    def test_private_key_content_detected(self):
        # Build the header at runtime so the test source file itself does
        # not contain the literal private key marker that the boundary
        # scanner looks for.
        dash5 = "-" * 5
        key_header = f"{dash5}BEGIN RSA PRIVATE KEY{dash5}"
        key_footer = f"{dash5}END RSA PRIVATE KEY{dash5}"
        (self.tmp_path / "config.txt").write_text(
            f"{key_header}\nfake\n{key_footer}\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "-A"], cwd=self.tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add key content"],
            cwd=self.tmp_path,
            check=True,
        )
        with self.assertRaises(BOUNDARY.PublicationBoundaryError) as ctx:
            BOUNDARY.validate_boundary(self.tmp_path, self.excl_path)
        self.assertIn("config.txt", str(ctx.exception))

    def test_source_tree_hash_deterministic(self):
        (self.tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add a"],
            cwd=self.tmp_path,
            check=True,
        )
        publishable, _ = BOUNDARY.validate_boundary(self.tmp_path, self.excl_path)
        h1 = BOUNDARY.source_tree_hash(self.tmp_path, publishable)
        h2 = BOUNDARY.source_tree_hash(self.tmp_path, publishable)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_source_tree_hash_changes_with_content(self):
        (self.tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add a"],
            cwd=self.tmp_path,
            check=True,
        )
        publishable, _ = BOUNDARY.validate_boundary(self.tmp_path, self.excl_path)
        h1 = BOUNDARY.source_tree_hash(self.tmp_path, publishable)
        (self.tmp_path / "a.py").write_text("a = 2\n", encoding="utf-8")
        h2 = BOUNDARY.source_tree_hash(self.tmp_path, publishable)
        self.assertNotEqual(h1, h2)

    def test_source_tree_hash_changes_with_tracked_file_mode(self):
        launcher = self.tmp_path / "review-sensei"
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add launcher"],
            cwd=self.tmp_path,
            check=True,
        )
        publishable, _ = BOUNDARY.validate_boundary(self.tmp_path, self.excl_path)
        first_hash = BOUNDARY.source_tree_hash(self.tmp_path, publishable)

        subprocess.run(
            ["git", "update-index", "--chmod=+x", "--", "review-sensei"],
            cwd=self.tmp_path,
            check=True,
        )
        second_hash = BOUNDARY.source_tree_hash(self.tmp_path, publishable)

        self.assertNotEqual(first_hash, second_hash)


class PublishScriptTests(unittest.TestCase):
    @unittest.skipIf(
        sys.platform == "win32",
        "Windows filesystems do not represent POSIX executable modes",
    )
    def test_export_source_commit_preserves_executable_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_git_repo(repo)
            executable = repo / "bin" / "review-sensei"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(
                ["git", "update-index", "--chmod=+x", "--", "bin/review-sensei"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "add executable"],
                cwd=repo,
                check=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            destination = Path(tmp) / "export"

            publish = load_publish_script()
            files = publish._export_source_commit(repo, sha, destination)

            self.assertIn("bin/review-sensei", files)
            exported = destination / "bin" / "review-sensei"
            self.assertTrue(exported.stat().st_mode & stat.S_IXUSR)
            self.assertEqual(
                exported.read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n"
            )

    def test_public_commit_identity_uses_configured_username(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            publish = load_publish_script()
            publish._configure_public_identity(repo)
            name = subprocess.run(
                ["git", "config", "--get", "user.name"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            email = subprocess.run(
                ["git", "config", "--get", "user.email"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(name, publish.PUBLIC_COMMIT_NAME)
            self.assertEqual(email, publish.PUBLIC_COMMIT_EMAIL)

    def test_dry_run_validates_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            excl_path = tmp_path / ".publication" / "exclusions.json"
            _write_exclusions(excl_path, [])
            _secret_header = "-" * 5
            key_blob = (
                f"{_secret_header}BEGIN RSA PRIVATE KEY{_secret_header}\n"
                "not a real key\n"
                f"{_secret_header}END RSA PRIVATE KEY{_secret_header}\n"
            )
            (tmp_path / "credentials.txt").write_text(key_blob, encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "add credentials"],
                cwd=tmp_path,
                check=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            publish = load_publish_script()
            with self.assertRaises(publish.PublicationBoundaryError):
                publish.sync_to_public(
                    source_root=tmp_path,
                    source_sha=sha,
                    public_repo="malsabbagh/review-sensei",
                    public_remote="",
                    dry_run=True,
                )

    def test_invalid_public_branch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            excl_path = tmp_path / ".publication" / "exclusions.json"
            _write_exclusions(excl_path, [])
            (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "add hello"],
                cwd=tmp_path,
                check=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            publish = load_publish_script()
            with self.assertRaises(publish.PublicationSyncError):
                publish.sync_to_public(
                    source_root=tmp_path,
                    source_sha=sha,
                    public_repo="malsabbagh/review-sensei",
                    public_remote="",
                    public_branch="$(rm -rf /)",
                    dry_run=True,
                )

    def test_dry_run_returns_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            excl_path = tmp_path / ".publication" / "exclusions.json"
            _write_exclusions(excl_path, [])
            (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "add hello"],
                cwd=tmp_path,
                check=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            publish = load_publish_script()
            entry = publish.sync_to_public(
                source_root=tmp_path,
                source_sha=sha,
                public_repo="malsabbagh/review-sensei",
                public_remote="",
                dry_run=True,
            )
            self.assertEqual(entry["source_sha"], sha)
            self.assertEqual(entry["public_sha"], "(dry-run)")
            self.assertEqual(entry["public_repo"], "malsabbagh/review-sensei")
            self.assertEqual(
                entry["source_tree_hash_algorithm"], BOUNDARY.TREE_HASH_ALGORITHM
            )


def _write_policy(path: Path, **overrides) -> None:
    policy = {
        "version": 1,
        "history_strategy": "clean-root",
        "public_identity": {
            "emails": ["test@example.com"],
            "names": ["malsabbagh", "Test"],
        },
        "non_identity_handles": ["sensei"],
        "path_classifications": {
            "synthetic": ["evaluation/**", "tests/fixtures/**", "examples/**"],
            "public": ["README.md", "docs/**", "src/**", ".github/**"],
        },
        "prohibited_path_classes": [
            {
                "class": "secrets",
                "patterns": [".env", ".env.*", "**/secrets/**", "**/*.pem"],
            },
            {
                "class": "private-fixtures",
                "patterns": [".project-ai/**", ".agents/**", ".codex/**"],
            },
            {
                "class": "internal-endpoints",
                "patterns": ["**/.internal/**", "**/internal/**"],
            },
            {
                "class": "private-repo-metadata",
                "patterns": [".publication/**", "AGENTS.md"],
            },
        ],
        "prohibited_content_classes": [
            "private-keys",
            "tokens",
            "private-repo-urls",
            "private-network-endpoints",
            "identity-metadata",
        ],
        "redacted_report": {
            "include_paths": True,
            "include_rule": True,
            "include_category": True,
            "include_value": False,
        },
        "allowed_endpoints": ["127.0.0.1", "localhost"],
    }
    policy.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy), encoding="utf-8")


class PublicationAuditTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        _init_git_repo(self.tmp_path)
        self.policy_path = self.tmp_path / ".publication" / "audit-policy.json"
        _write_policy(self.policy_path)
        self.excl_path = self.tmp_path / ".publication" / "exclusions.json"
        _write_exclusions(
            self.excl_path,
            [
                {"pattern": ".publication/**", "reason": "internal control files"},
                {"pattern": "AGENTS.md", "reason": "internal agent instructions"},
            ],
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _commit(self, *paths: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=self.tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "test commit"],
            cwd=self.tmp_path,
            check=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_exact_commit_selection(self):
        (self.tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["source_sha"], sha)
        self.assertEqual(report["counts"]["blocking_findings"], 0)
        self.assertTrue(report["clean_root_history"])
        counts = report["counts"]
        self.assertGreater(counts["total_bytes"], 0)
        self.assertEqual(
            counts["total_bytes"],
            counts["excluded_bytes"] + counts["publishable_bytes"],
        )
        self.assertEqual(counts["synthetic_bytes"], 0)

    def test_symbolic_source_ref_rejected(self):
        (self.tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        self._commit()
        with self.assertRaises(AUDIT.PublicationAuditError):
            AUDIT.audit_publication(
                self.tmp_path, "HEAD", self.policy_path, self.excl_path
            )

    def test_private_repo_reference_rejected(self):
        private_slug = "malsabbagh" + "/" + "code-sensei"
        (self.tmp_path / "README.md").write_text(
            f"private repo {private_slug}\n", encoding="utf-8"
        )
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 1)
        self.assertEqual(report["findings"][0]["category"], "private-repo-urls")
        self.assertNotIn(private_slug, json.dumps(report))

    def test_private_key_rejected(self):
        dash5 = "-" * 5
        key = f"{dash5}BEGIN RSA PRIVATE KEY{dash5}\nfake\n{dash5}END RSA PRIVATE KEY{dash5}\n"
        (self.tmp_path / "config.txt").write_text(key, encoding="utf-8")
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 1)
        self.assertEqual(report["findings"][0]["category"], "private-keys")

    def test_private_network_endpoint_rejected(self):
        endpoint = "http://" + "10.0.0.5" + ":8080/api"
        (self.tmp_path / "config.txt").write_text(f"{endpoint}\n", encoding="utf-8")
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 1)
        self.assertEqual(report["findings"][0]["category"], "private-network-endpoints")

    def test_loopback_endpoint_allowed(self):
        (self.tmp_path / "config.txt").write_text(
            "http://127.0.0.1:11434/api\n", encoding="utf-8"
        )
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 0)

    def test_synthetic_fixture_scanned_for_credentials(self):
        token = "ghp_" + "123456789012345678901234567890123456"
        (self.tmp_path / "evaluation").mkdir()
        (self.tmp_path / "evaluation" / "fixture.txt").write_text(
            f"{token}\n", encoding="utf-8"
        )
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 1)
        self.assertEqual(report["findings"][0]["category"], "tokens")

    def test_allowlisted_synthetic_identity_not_blocked(self):
        (self.tmp_path / "evaluation").mkdir()
        (self.tmp_path / "evaluation" / "fixture.txt").write_text(
            "test@example.com @malsabbagh\n", encoding="utf-8"
        )
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 0)

    def test_public_syntax_not_treated_as_identity_metadata(self):
        policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        policy["public_identity"]["emails"].append(
            "41898282+github-actions[bot]@users.noreply.github.com"
        )
        self.policy_path.write_text(json.dumps(policy), encoding="utf-8")
        (self.tmp_path / "docs").mkdir()
        (self.tmp_path / "docs" / "guide.html").write_text(
            '{"@context": "https://schema.org", "@type": "SoftwareApplication"}\n'
            "@media (max-width: 10px) {}\n"
            "@cloudflare/workers-types\n"
            "generated by 41898282+github-actions[bot]@users.noreply.github.com\n",
            encoding="utf-8",
        )
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 0)

    def test_declared_product_command_not_treated_as_identity_metadata(self):
        (self.tmp_path / "docs").mkdir()
        (self.tmp_path / "docs" / "guide.md").write_text(
            "Reply with @sensei to request another review.\n", encoding="utf-8"
        )
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 0)

    def test_package_version_syntax_not_treated_as_identity_metadata(self):
        (self.tmp_path / "docs").mkdir()
        (self.tmp_path / "docs" / "guide.md").write_text(
            "Use @reviewsensei/cli@0.1.0 with npm@11.5.1.\n",
            encoding="utf-8",
        )
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 0)

    def test_numeric_only_handle_is_treated_as_identity_metadata(self):
        (self.tmp_path / "docs").mkdir()
        (self.tmp_path / "docs" / "guide.md").write_text(
            "Contact @123 for access.\n", encoding="utf-8"
        )
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 1)
        self.assertEqual(report["findings"][0]["rule"], "unallowlisted-handle")

    def test_numeric_leading_hyphen_handle_is_treated_as_identity_metadata(self):
        (self.tmp_path / "docs").mkdir()
        (self.tmp_path / "docs" / "guide.md").write_text(
            "Contact @123-private for access.\n", encoding="utf-8"
        )
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 1)
        self.assertEqual(report["findings"][0]["rule"], "unallowlisted-handle")

    def test_malformed_non_identity_handle_rejected(self):
        policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        policy["non_identity_handles"] = ["@sensei"]
        self.policy_path.write_text(json.dumps(policy), encoding="utf-8")
        sha = self._commit()
        with self.assertRaises(AUDIT.PublicationAuditError):
            AUDIT.audit_publication(
                self.tmp_path, sha, self.policy_path, self.excl_path
            )

    def test_unallowlisted_identity_metadata_rejected(self):
        email = "someone@" + "private.example"
        (self.tmp_path / "docs").mkdir()
        (self.tmp_path / "docs" / "guide.md").write_text(
            f"contact {email} @someone\n", encoding="utf-8"
        )
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 2)
        self.assertTrue(
            all(f["category"] == "identity-metadata" for f in report["findings"])
        )

    def test_report_redaction(self):
        token = "ghp_" + "123456789012345678901234567890123456"
        (self.tmp_path / "config.txt").write_text(f"{token}\n", encoding="utf-8")
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertNotIn("ghp_", json.dumps(report))
        self.assertNotIn("123456789012345678901234567890123456", json.dumps(report))

    def test_deterministic_reruns(self):
        (self.tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        sha = self._commit()
        r1 = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        r2 = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(r1["report_digest"], r2["report_digest"])
        self.assertEqual(r1["publishable_tree_hash"], r2["publishable_tree_hash"])
        self.assertEqual(
            r1["publishable_tree_hash_algorithm"], BOUNDARY.TREE_HASH_ALGORITHM
        )

    def test_audit_publishable_hash_changes_with_tracked_file_mode(self):
        launcher = self.tmp_path / "review-sensei"
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        first_sha = self._commit()
        first_report = AUDIT.audit_publication(
            self.tmp_path, first_sha, self.policy_path, self.excl_path
        )

        subprocess.run(
            ["git", "update-index", "--chmod=+x", "--", "review-sensei"],
            cwd=self.tmp_path,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "make launcher executable"],
            cwd=self.tmp_path,
            check=True,
        )
        second_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        second_report = AUDIT.audit_publication(
            self.tmp_path, second_sha, self.policy_path, self.excl_path
        )

        self.assertNotEqual(
            first_report["publishable_tree_hash"],
            second_report["publishable_tree_hash"],
        )
        self.assertEqual(
            second_report["publishable_tree_hash_algorithm"],
            BOUNDARY.TREE_HASH_ALGORITHM,
        )
        if sys.platform == "win32":
            # Git's Windows worktree/index handling cannot materialize the
            # executable mode that the audit reads directly from the commit.
            return
        publishable, _ = BOUNDARY.validate_boundary(self.tmp_path, self.excl_path)
        self.assertEqual(
            second_report["publishable_tree_hash"],
            BOUNDARY.source_tree_hash(self.tmp_path, publishable),
        )

    def test_malformed_policy_rejected(self):
        self.policy_path.write_text('{"version": 2}', encoding="utf-8")
        sha = self._commit()
        with self.assertRaises(AUDIT.PublicationAuditError):
            AUDIT.audit_publication(
                self.tmp_path, sha, self.policy_path, self.excl_path
            )

    def test_missing_prohibited_content_category_rejected(self):
        policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        policy["prohibited_content_classes"].remove("tokens")
        self.policy_path.write_text(json.dumps(policy), encoding="utf-8")
        sha = self._commit()
        with self.assertRaises(AUDIT.PublicationAuditError) as ctx:
            AUDIT.audit_publication(
                self.tmp_path, sha, self.policy_path, self.excl_path
            )
        self.assertIn("missing", str(ctx.exception))

    def test_unknown_prohibited_content_category_rejected(self):
        policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        policy["prohibited_content_classes"].append("unknown-class")
        self.policy_path.write_text(json.dumps(policy), encoding="utf-8")
        sha = self._commit()
        with self.assertRaises(AUDIT.PublicationAuditError) as ctx:
            AUDIT.audit_publication(
                self.tmp_path, sha, self.policy_path, self.excl_path
            )
        self.assertIn("unknown", str(ctx.exception))

    def test_unsafe_non_blob_entry_rejected(self):
        (self.tmp_path / "submodule").mkdir()
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{head_sha},submodule",
            ],
            cwd=self.tmp_path,
            check=True,
        )
        sha = self._commit()
        report = AUDIT.audit_publication(
            self.tmp_path, sha, self.policy_path, self.excl_path
        )
        self.assertEqual(report["counts"]["blocking_findings"], 1)
        self.assertEqual(report["findings"][0]["category"], "unsafe-tree-entry")


class PublicationDocumentationTests(unittest.TestCase):
    def test_public_registration_guide_omits_private_upstream_slug(self):
        content = (ROOT / "docs" / "github-app-registration.md").read_text(
            encoding="utf-8"
        )
        private_slug = "malsabbagh" + "/" + "code-sensei"
        self.assertNotIn(private_slug, content)
        self.assertIn("internal development repositories", content)


if __name__ == "__main__":
    unittest.main()
