"""Validation tests for the frozen v6 one-writer-per-PR contract and Phase 0-1 docs."""

from __future__ import annotations

import io
import json
import re
import unittest
from pathlib import Path
from urllib.error import HTTPError

from review_sensei_github.v6_recovery_ledger import (
    INTENT_SCHEMA,
    PROGRESS_HINT_SCHEMA,
    RECEIPT_SCHEMA,
    UNRESOLVED_RESULT_SCHEMA,
    AdvisoryProgressHint,
    CompletionReceiptRecord,
    UnresolvedResultRecord,
    WriteIntentRecord,
    record_to_dict,
)

from review_sensei.broker_diagnostics import BrokerDiagnostic, parse_broker_diagnostic

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = (
    ROOT / "src" / "review_sensei" / "schemas" / "v6-coordinator-contract.json"
)
DIAGNOSTICS_PATH = (
    ROOT / "src" / "review_sensei" / "schemas" / "broker-diagnostics.json"
)
PROPOSAL_PATH = (
    ROOT / "docs" / "proposals" / "v6-one-writer-per-pr-audit-and-contract.md"
)
ADR_PATH = ROOT / "docs" / "adr" / "0145-v6-one-writer-per-pr-coordinator.md"
ADR_INDEX_PATH = ROOT / "docs" / "adr" / "README.md"

EXPECTED_DIAGNOSTIC_CODES = frozenset(
    {
        "broker_v6_pilot_v5_writer_disabled",
        "v6_lease_reserved",
        "v6_lease_active_other_owner",
        "v6_lease_draining",
        "v6_lease_reconciling",
        "v6_lease_hold",
        "v6_lease_head_changed",
        "v6_lease_predecessor_not_terminal",
        "v6_lease_revocation_uncertain",
        "v6_lease_escrow_lost",
        "v6_lease_clock_untrusted",
        "v6_lease_slot_continuity_unproved",
        "v6_lease_unauthorized_tuple",
        "v6_lease_rollback_blocked",
        "v6_lease_mint_uncertain",
    }
)

EXPECTED_STATES = (
    "IDLE",
    "RESERVED",
    "MINTING",
    "ACTIVE",
    "DRAINING",
    "RECONCILING",
    "HOLD",
)

EXPECTED_ROUTES = frozenset(
    {
        "/github/v6/lease/acquire",
        "/github/v6/lease/drain",
        "/github/v6/lease/complete-reconcile",
        "/github/v6/lease/release",
        "/github/v6/lease/status",
        "/github/v6/lease/resolve-hold",
    }
)


class V6CoordinatorContractTests(unittest.TestCase):
    """Verify frozen Phase 1 contract, diagnostics catalog, and docs invariants."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        cls.diagnostics = json.loads(DIAGNOSTICS_PATH.read_text(encoding="utf-8"))

    def test_baseline_and_v5_pin_preserved(self) -> None:
        self.assertEqual(self.contract["schema_version"], "v6-coordinator-contract-v1")
        baseline = self.contract["baseline"]
        self.assertEqual(
            baseline["pinned_main_sha"],
            "ff406a44754d5016fead2435e547b9fd0536e17b",
        )
        self.assertEqual(baseline["pinned_v5_tag"], "v5")
        self.assertEqual(
            baseline["pinned_v5_commit"],
            "40b4f52face3402fc3a9cd51387bde7cf53c3951",
        )
        self.assertEqual(baseline["pinned_engine_version"], "0.8.28")
        self.assertEqual(baseline["pinned_github_package_version"], "0.3.34")

        wrangler = json.loads(
            (ROOT / "deploy" / "cloudflare" / "wrangler.jsonc").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(wrangler["vars"]["PUBLIC_WORKFLOW_TAG"], "v5")
        self.assertEqual(
            self.contract["pilot_isolation"]["v5_writer_disabled_diagnostic"],
            "broker_v6_pilot_v5_writer_disabled",
        )
        self.assertEqual(
            self.contract["pilot_isolation"]["pre_admission_drain_seconds"], 3600
        )

    def test_slot_key_template_and_pattern(self) -> None:
        slot = self.contract["slot_key"]
        self.assertEqual(
            slot["template"],
            "v6-pr:{host}:{app_id}:{installation_id}:{base_repo_id}:{pr_number}",
        )
        self.assertEqual(
            slot["components"],
            ["host", "app_id", "installation_id", "base_repo_id", "pr_number"],
        )
        pattern = re.compile(slot["pattern"])
        self.assertIsNotNone(pattern.fullmatch("v6-pr:github.com:123:456:789:42"))
        for bad in (
            "v6-pr:github.com:0:456:789:42",
            "v6-pr:github.com:123:456:789",
            "v5-pr:github.com:123:456:789:42",
            "v6-pr:github.com:123:456:789:42:extra",
        ):
            self.assertIsNone(pattern.fullmatch(bad), bad)

    def test_state_machine_transitions_are_complete_and_safe(self) -> None:
        states = tuple(self.contract["coordinator_states"])
        self.assertEqual(states, EXPECTED_STATES)
        transitions = self.contract["state_transitions"]
        self.assertEqual(set(transitions.keys()), set(states))
        for state, targets in transitions.items():
            self.assertTrue(targets, f"{state} must have at least one outgoing edge")
            self.assertEqual(len(targets), len(set(targets)))
            for target in targets:
                self.assertIn(target, states)
        # HOLD cannot bypass reconciliation/release directly into ACTIVE or MINTING.
        self.assertNotIn("ACTIVE", transitions["HOLD"])
        self.assertNotIn("MINTING", transitions["HOLD"])
        self.assertNotIn("RESERVED", transitions["HOLD"])

    def test_keep_vs_never_keep_disjointness_and_secret_bans(self) -> None:
        coord = self.contract["coordinator_storage"]
        coord_keep = set(coord["keep_fields"])
        coord_never = set(coord["never_keep_fields"])
        self.assertTrue(coord_keep)
        self.assertTrue(coord_never)
        self.assertEqual(coord_keep & coord_never, set())
        self.assertEqual(coord_keep, set(coord["slot_state_fields"].keys()))
        for banned in (
            "github_app_private_key",
            "reviewsensei_signing_key",
            "raw_aes_key",
            "oidc_token",
            "plaintext_installation_token",
            "workflow_github_token",
            "provider_api_key",
            "pr_diff",
        ):
            self.assertIn(banned, coord_never)

        ledger = self.contract["pr_ledger"]
        ledger_keep = set(ledger["keep_fields"])
        ledger_never = set(ledger["never_keep_fields"])
        self.assertTrue(ledger_keep)
        self.assertTrue(ledger_never)
        self.assertEqual(ledger_keep & ledger_never, set())
        for banned in (
            "github_app_private_key",
            "reviewsensei_signing_key",
            "raw_aes_key",
            "oidc_token",
            "plaintext_installation_token",
            "encrypted_escrow",
            "iv_b64",
            "ciphertext_b64",
            "workflow_github_token",
            "provider_api_key",
        ):
            self.assertIn(banned, ledger_never)

        all_schema_fields = set()
        for entry in ledger["record_schemas"].values():
            all_schema_fields.update(entry["required_fields"])
            self.assertEqual(
                set(entry["required_fields"]), set(entry["properties"].keys())
            )
        self.assertEqual(ledger_keep, all_schema_fields)

    def test_pr_ledger_schemas_match_v6_recovery_ledger_module(self) -> None:
        schemas = self.contract["pr_ledger"]["record_schemas"]
        self.assertEqual(
            set(schemas.keys()),
            {"owner", "intent", "receipt", "hold", "unresolved", "progress"},
        )
        self.assertEqual(schemas["owner"]["schema"], "reviewsensei:v6:owner:v1")
        self.assertEqual(schemas["hold"]["schema"], "reviewsensei:v6:hold:v1")
        self.assertEqual(schemas["intent"]["schema"], INTENT_SCHEMA)
        self.assertEqual(schemas["receipt"]["schema"], RECEIPT_SCHEMA)
        self.assertEqual(schemas["unresolved"]["schema"], UNRESOLVED_RESULT_SCHEMA)
        self.assertEqual(schemas["progress"]["schema"], PROGRESS_HINT_SCHEMA)

        sample_intent = WriteIntentRecord(
            record_id="rec-1",
            operation_id="op-1",
            writer_epoch=1,
            pr_head_sha="a" * 40,
            effect_kind="pr_review_publish",
            payload_sha256="b" * 64,
            target_digest="c" * 64,
        )
        sample_receipt = CompletionReceiptRecord(
            record_id="rec-2",
            operation_id="op-1",
            writer_epoch=1,
            github_effect_id=101,
            payload_sha256="b" * 64,
            completed_at="2026-11-27T00:00:00Z",
        )
        sample_unresolved = UnresolvedResultRecord(
            record_id="rec-3",
            operation_id="op-1",
            pr_head_sha="a" * 40,
            payload_sha256="d" * 64,
            findings_payload=({"id": "f1"},),
            required_evidence_digests=("e" * 64,),
            artifact_cache_sha256=None,
        )
        sample_progress = AdvisoryProgressHint(
            operation_id="op-1",
            recovery_phase="scan",
            cursor_hint=None,
            verified_prefix_count=0,
            verified_prefix_digest="",
            last_verified_id=None,
            stall_count=0,
            last_progress_digest="",
            transient_error_count=0,
        )
        for key, record in (
            ("intent", sample_intent),
            ("receipt", sample_receipt),
            ("unresolved", sample_unresolved),
            ("progress", sample_progress),
        ):
            self.assertEqual(
                set(record_to_dict(record).keys()),
                set(schemas[key]["required_fields"]),
            )

    def test_escrow_revocation_and_routes_contract(self) -> None:
        coord = self.contract["coordinator_storage"]
        self.assertEqual(coord["clock_margin_ms_default"], 300000)
        escrow = coord["escrow"]
        self.assertEqual(escrow["kdf"], "HKDF-SHA256")
        self.assertEqual(escrow["cipher"], "AES-GCM-256")
        self.assertEqual(escrow["key_root_env"], "REVIEWSENSEI_SIGNING_KEY")
        self.assertEqual(
            escrow["aad_fields"], ["slot_key", "generation", "reservation_id"]
        )

        revocation = self.contract["revocation_rules"]
        self.assertEqual(revocation["endpoint"], "DELETE /installation/token")
        self.assertEqual(revocation["clock_margin_ms_default"], 300000)
        outcomes = revocation["outcomes"]
        self.assertEqual(outcomes["http_204"]["next_state"], "IDLE")
        self.assertIn(
            "clock_margin_ms", outcomes["http_401"]["expiry_fallback_condition"]
        )
        self.assertTrue(
            outcomes["http_429_or_ratelimit_403"]["requires_rate_limit_indicator"]
        )
        self.assertFalse(outcomes["permission_403"]["requires_rate_limit_indicator"])
        self.assertEqual(outcomes["permission_403"]["action"], "fail_closed_no_retry")
        self.assertTrue(outcomes["http_5xx_or_timeout"]["includes_timeout"])

        routes = self.contract["http_routes"]
        self.assertEqual(set(routes.keys()), EXPECTED_ROUTES)
        contract_codes = set(self.contract["diagnostic_codes"])
        for route, spec in routes.items():
            self.assertEqual(spec["method"], "POST", route)
            self.assertIn("oidc_token", spec["request_fields"]["required"], route)
            self.assertTrue(set(spec["reason_codes"]).issubset(contract_codes), route)

    def test_broker_diagnostics_covers_all_contract_codes(self) -> None:
        contract_codes = set(self.contract["diagnostic_codes"])
        self.assertEqual(contract_codes, EXPECTED_DIAGNOSTIC_CODES)

        for code in sorted(EXPECTED_DIAGNOSTIC_CODES):
            with self.subTest(code=code):
                self.assertIn(code, self.diagnostics)
                entry = self.diagnostics[code]
                self.assertEqual(set(entry.keys()), {"stage", "action", "hint"})
                for field_name in ("stage", "action", "hint"):
                    value = entry[field_name]
                    self.assertIsInstance(value, str)
                    self.assertTrue(value.strip())
                    self.assertNotIn("\n", value)
                self.assertLessEqual(len(entry["stage"]), 64)
                self.assertLessEqual(len(entry["action"]), 64)

                diagnostic = BrokerDiagnostic(code)
                self.assertEqual(diagnostic.code, code)
                body = json.dumps(
                    {
                        "error": "v6_lease_refused",
                        "diagnostic": {
                            "version": 1,
                            "code": code,
                            "stage": diagnostic.stage,
                            "action": diagnostic.action,
                        },
                    }
                ).encode("utf-8")
                err = HTTPError(
                    "https://broker.invalid/github/v6/lease/acquire",
                    409,
                    "Conflict",
                    {"cf-ray": "0123456789abcdef-YYZ"},
                    io.BytesIO(body),
                )
                parsed = parse_broker_diagnostic(err)
                self.assertEqual(parsed.code, code)
                self.assertEqual(parsed.stage, diagnostic.stage)
                self.assertEqual(parsed.action, diagnostic.action)

    def test_blockers_and_doc_links_are_valid(self) -> None:
        blockers = self.contract["blockers"]
        self.assertEqual(
            [item["id"] for item in blockers],
            [
                "callable_v6_workflow",
                "fenced_issuance",
                "reproducible_ambiguous_write_recovery",
                "opt_in_github_qualification",
            ],
        )
        for item in blockers:
            self.assertEqual(item["status"], "OPEN")

        self.assertTrue(PROPOSAL_PATH.is_file())
        self.assertTrue(ADR_PATH.is_file())
        self.assertIn(
            "[0145](0145-v6-one-writer-per-pr-coordinator.md)",
            ADR_INDEX_PATH.read_text(encoding="utf-8"),
        )

        link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        for doc_path in (PROPOSAL_PATH, ADR_PATH):
            content = doc_path.read_text(encoding="utf-8")
            self.assertNotIn("file://", content)
            self.assertNotIn("/Users/", content)
            for target in link_pattern.findall(content):
                if target.startswith(("http://", "https://", "#")):
                    continue
                resolved = (doc_path.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(
                    resolved.exists(),
                    f"broken relative link {target!r} in {doc_path}",
                )


if __name__ == "__main__":
    unittest.main()
