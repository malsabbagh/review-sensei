"""G34 qualification: terminal action-result representability, pre-effect guard, and recovery."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github import review_run
from review_sensei_github.action_representability import (
    guard_current_command_representability,
)
from review_sensei_github.api_run_history import (
    ActionResultAncestryObservation,
    ActionResultReadContext,
    ArchivedActionResultObservation,
    read_action_result,
    read_ancestor_action_results,
    read_current_action_result,
)
from review_sensei_github.publication import parse_finding_index
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)
from test_action_run import CommandEntryFixture
from test_api_work_persistence import HEAD, IDENTITY, json_response

from review_sensei.api_work import (
    ACCOUNTING_VERSION,
    LEGACY_ACCOUNTING_VERSION,
    PARTITIONED_ACTION_ACCOUNTING_VERSION,
    ApiRunAccounting,
)
from review_sensei.bounded_evidence import (
    MAX_MANIFEST_BYTES,
    EvidenceReadBudget,
    canonical_bytes,
)
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.disposition import (
    MaintainerCommand,
    MaintainerCommandResult,
    apply_session_command,
    incomplete_coverage_from_result,
)
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult
from review_sensei.session import (
    MAX_DISPOSITION_BYTES,
    MAX_SESSION_COMMENT_BYTES,
    MAX_SESSION_RECORD_BYTES,
    LocalSessionLedger,
    SessionRecord,
    disposition_array_bytes,
)
from review_sensei.terminal_representability import (
    ActionSettlementPricing,
    TerminalRepresentabilityError,
    accounting_version_from_record,
    action_intent_sha256,
    build_action_result_binding,
    estimate_session_comment_bytes,
    estimate_session_record_bytes,
    evaluate_terminal_action_representability,
    preview_session_command_representability,
    require_terminal_action_representable,
    with_terminal_action_manifest,
)

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"


def _deterministic_valid_path(index: int, *, total_len: int) -> str:
    """Generate a valid high-entropy repository path of exact UTF-8 byte length."""

    prefix = "service"
    rem = total_len - len(prefix) - 1 - 3  # prefix + "/" + body + ".py"
    buf = bytearray()
    block = 0
    while len(buf) < rem:
        digest = hashlib.sha256(f"0:{index}:{block}".encode("ascii")).digest()
        for byte_val in digest:
            buf.append(ord(_ALPHABET[byte_val & 63]))
        block += 1
    tail = list(buf[:rem].decode("ascii"))
    if tail[0] in "._-":
        tail[0] = "a"
    if tail[-1] in "/.":
        tail[-1] = "z"
    candidate = f"{prefix}/{''.join(tail)}.py"
    assert len(candidate.encode("utf-8")) == total_len
    return candidate


def _multisegment_path(total_len: int) -> str:
    """Generate a valid multisegment path where every segment is <= 100 chars."""

    segments = [
        "services_" + ("a" * 60),
        "domain_" + ("b" * 60),
        "module_" + ("c" * 60),
        "adapter_" + ("d" * 60),
    ]
    joined = "/".join(segments)
    rem = total_len - len(joined) - 1 - 3
    assert 1 <= rem <= 90
    path = f"{joined}/{'e' * rem}.py"
    assert len(path.encode("utf-8")) == total_len
    return path


class _G34CommandFixture(CommandEntryFixture):
    def __init__(self, directory: str, *, limit: int = 1024) -> None:
        super().__init__(directory, limit=limit)
        subprocess.run(
            ["git", "config", "core.autocrlf", "false"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

    def external(self, request, timeout):  # type: ignore[no-untyped-def]
        response = super().external(request, timeout)
        path = urlsplit(request.full_url).path
        if (
            not self.command_active
            and "/actions/runs/" in path
            and path.endswith("/jobs")
        ):
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            if run_id in self.past_jobs:
                value = json.loads(response.read(524288))
                value["jobs"][0]["name"] = "ReviewSensei / " + self.past_jobs[run_id]
                return json_response(value)
        return response


class TerminalRepresentabilityG34Tests(unittest.TestCase):
    """Verify exact Section 20 G34 witness, early refusals, #431/#436 controls, and recovery."""

    def setUp(self) -> None:
        for target in ("socket.create_connection", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("unexpected network"))
            guard.start()
            self.addCleanup(guard.stop)

    def _default_binding(self, intent_sha256: str | None = None) -> dict[str, object]:
        digest = intent_sha256 or ("1" * 64)
        return build_action_result_binding(
            identity=IDENTITY,
            policy_commit="a" * 40,
            head_sha="b" * 40,
            intent_sha256=digest,
            configuration_digest="d" * 64,
            owner_generation=1,
            producer="github-bot:999",
        )

    def test_exact_section_20_g34_witness_byte_counts_and_pre_effect_refusal(
        self,
    ) -> None:
        """Preserve the 39-entry historical component witness as an independent control.

        Note: This synthetic component witness exercises the pre-effect guard on a
        single unrecorded command and does NOT establish a live post-effect loss.
        """
        files = tuple(
            FileCoverage(
                path=_deterministic_valid_path(i, total_len=227),
                outcome="budget-exhausted",
            )
            for i in range(38)
        )
        partial_result = ReviewResult(
            summary="Partial review under budget ceiling",
            comments=(),
            provider="openai",
            review_status="partial",
            coverage=CoverageManifest(
                files=files,
                enumerated_paths=tuple(f.path for f in files),
            ),
        )
        incomplete = incomplete_coverage_from_result(partial_result)
        self.assertEqual(len(incomplete), 39)
        file_tokens = [tok for tok in incomplete if tok.startswith("file:")]
        result_tokens = [tok for tok in incomplete if tok.startswith("result:")]
        self.assertEqual(len(file_tokens), 38)
        self.assertEqual(len(result_tokens), 1)
        self.assertTrue(all(len(tok.encode("utf-8")) == 249 for tok in file_tokens))
        self.assertEqual(len(result_tokens[0].encode("utf-8")), 71)

        fixed_now = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)
        initial_record = SessionRecord.create(IDENTITY, now=fixed_now)
        command = MaintainerCommand(
            action="accept-coverage-uncertainty",
            actor="alice",
            reason="accepted after full manual audit of the PR!!",
            head_sha="b" * 40,
        )
        intent_sha = action_intent_sha256({"purpose": "command", "source_id": 41})
        binding = self._default_binding(intent_sha)

        preview_res, preview_rec, report = preview_session_command_representability(
            identity=IDENTITY,
            current_record=initial_record,
            command=command,
            intent_sha256=intent_sha,
            binding=binding,
            actor_role="OWNER",
            policy_digest="c" * 64,
            provenance="comment:41",
            incomplete_coverage=incomplete,
            now=fixed_now,
        )

        self.assertIsNotNone(preview_res)
        self.assertIsNotNone(preview_rec)
        assert preview_rec is not None
        # Exact Section 20 G34 witness figures (never superseded 10,121 / 9,678):
        self.assertEqual(disposition_array_bytes(preview_rec.dispositions), 10_195)
        self.assertLessEqual(
            disposition_array_bytes(preview_rec.dispositions), MAX_DISPOSITION_BYTES
        )
        self.assertEqual(report.disposition_bytes, 10_195)
        self.assertEqual(report.envelope_bytes, 20_933)
        self.assertEqual(report.payload_bytes, 9_695)
        self.assertEqual(report.carrier_bytes, 9_750)
        self.assertGreater(report.carrier_bytes, MAX_MANIFEST_BYTES)
        self.assertIsNone(report.manifest_bytes)
        self.assertFalse(report.representable)
        self.assertEqual(report.refusal_code, "carrier_inline_bound_exceeded")
        self.assertFalse(report.authority)
        self.assertEqual(report.recovery.action, "review_remaining")
        self.assertEqual(report.recovery.command, "@reviewsensei review remaining")
        self.assertTrue(report.recovery.preserves_obligations)
        self.assertTrue(report.recovery.preserves_reservations)
        self.assertTrue(report.recovery.preserves_unknown_charges)
        # Original SessionRecord was never mutated by pre-effect preview:
        self.assertEqual(initial_record.dispositions, ())

        with self.assertRaises(TerminalRepresentabilityError) as ctx:
            require_terminal_action_representable(report)
        self.assertEqual(ctx.exception.report.carrier_bytes, 9_750)
        self.assertIn("@reviewsensei review remaining", str(ctx.exception))
        # Note: this 39-entry component witness (10,195B disposition, 20,933B envelope,
        # 9,750B carrier in this fixture / 9,690B in the reviewer's standalone probe vs 8,192B)
        # is an independent historical component control and does NOT establish a live post-effect loss.

    def test_early_refusal_304_byte_path_and_256_file_cap_controls(self) -> None:
        fixed_now = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)
        initial_record = SessionRecord.create(IDENTITY, now=fixed_now)
        command = MaintainerCommand(
            action="accept-coverage-uncertainty",
            actor="alice",
            reason="accepted after manual audit",
            head_sha="b" * 40,
        )
        binding = self._default_binding()

        # Control 1: 304-byte valid multisegment path -> 326-byte token (> 256 limit)
        path_304 = _multisegment_path(304)
        res_304 = ReviewResult(
            summary="Partial",
            comments=(),
            provider="openai",
            review_status="partial",
            coverage=CoverageManifest(
                files=(
                    FileCoverage(
                        path=path_304,
                        outcome="budget-exhausted",
                    ),
                ),
                enumerated_paths=(path_304,),
            ),
        )
        inc_304 = incomplete_coverage_from_result(res_304)
        file_tok_326 = next(tok for tok in inc_304 if tok.startswith("file:"))
        self.assertEqual(len(file_tok_326.encode("utf-8")), 326)
        p_res, p_rec, rep_304 = preview_session_command_representability(
            identity=IDENTITY,
            current_record=initial_record,
            command=command,
            intent_sha256="1" * 64,
            binding=binding,
            actor_role="OWNER",
            policy_digest="c" * 64,
            provenance="comment:42",
            incomplete_coverage=inc_304,
            now=fixed_now,
        )
        self.assertIsNone(p_res)
        self.assertIsNone(p_rec)
        self.assertFalse(rep_304.representable)
        self.assertEqual(rep_304.refusal_code, "disposition_token_bytes_exceeded")
        self.assertEqual(rep_304.max_disposition_token_bytes, 326)
        self.assertEqual(rep_304.recovery.command, "@reviewsensei review remaining")

        # Control 2: 256 file entries + 1 mandatory result token = 257 (> 256 cap)
        _files_256 = tuple(
            FileCoverage(
                path=f"src/mod_{i:03d}.py",
                outcome="budget-exhausted",
            )
            for i in range(256)
        )
        res_256 = ReviewResult(
            summary="Partial",
            comments=(),
            provider="openai",
            review_status="partial",
            coverage=CoverageManifest(
                files=_files_256,
                enumerated_paths=tuple(f.path for f in _files_256),
            ),
        )
        inc_257 = incomplete_coverage_from_result(res_256)
        self.assertEqual(len(inc_257), 257)
        p_res2, p_rec2, rep_257 = preview_session_command_representability(
            identity=IDENTITY,
            current_record=initial_record,
            command=command,
            intent_sha256="1" * 64,
            binding=binding,
            actor_role="OWNER",
            policy_digest="c" * 64,
            provenance="comment:43",
            incomplete_coverage=inc_257,
            now=fixed_now,
        )
        self.assertIsNone(p_res2)
        self.assertIsNone(p_rec2)
        self.assertFalse(rep_257.refusal_code == "carrier_inline_bound_exceeded")
        self.assertEqual(rep_257.refusal_code, "disposition_entry_cap_exceeded")
        self.assertEqual(rep_257.disposition_entry_count, 257)

        # Control 3: Inline disposition array > 10,240 bytes when snapshot staging disabled
        files_42 = tuple(
            FileCoverage(
                path=_deterministic_valid_path(i, total_len=227),
                outcome="budget-exhausted",
            )
            for i in range(42)
        )
        inc_42 = incomplete_coverage_from_result(
            ReviewResult(
                summary="Partial",
                comments=(),
                provider="openai",
                review_status="partial",
                coverage=CoverageManifest(
                    files=files_42,
                    enumerated_paths=tuple(f.path for f in files_42),
                ),
            )
        )
        _pr3, _rc3, rep_arr = preview_session_command_representability(
            identity=IDENTITY,
            current_record=initial_record,
            command=command,
            intent_sha256="1" * 64,
            binding=binding,
            actor_role="OWNER",
            policy_digest="c" * 64,
            provenance="comment:44",
            incomplete_coverage=inc_42,
            enable_disposition_snapshot_writes=False,
            now=fixed_now,
        )
        self.assertFalse(rep_arr.representable)
        self.assertEqual(rep_arr.refusal_code, "disposition_array_bytes_exceeded")

    def test_reader_observer_non_authority_and_v1_v2_v3_writer_boundaries(
        self,
    ) -> None:
        fixed_now = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)
        record = SessionRecord.create(IDENTITY, now=fixed_now)
        binding = self._default_binding()
        compact_result = asdict(
            MaintainerCommandResult(
                action="pause",
                applied=True,
                operator_paused=True,
                summary="paused by maintainer",
            )
        )

        # V1 and V2 writers remain supported and representable:
        for version in (LEGACY_ACCOUNTING_VERSION, ACCOUNTING_VERSION):
            rep = evaluate_terminal_action_representability(
                identity=IDENTITY,
                intent_sha256="1" * 64,
                outcome={"kind": "applied", "reason": None},
                result_document=compact_result,
                binding=binding,
                current_record=record,
                accounting_version=version,
            )
            self.assertTrue(rep.representable)
            self.assertIsNone(rep.refusal_code)
            self.assertEqual(
                estimate_session_comment_bytes(
                    record,
                    repository_id=IDENTITY.repository_id or 123,
                    pull_request=IDENTITY.pull_request,
                ),
                len(
                    render_session_comment(
                        repository_id=IDENTITY.repository_id or 123,
                        pull_request=IDENTITY.pull_request,
                        record=record,
                    ).encode("utf-8")
                ),
            )

        # V3 writer activation is default-disabled per Section 8.3:
        rep_v3 = evaluate_terminal_action_representability(
            identity=IDENTITY,
            intent_sha256="1" * 64,
            outcome={"kind": "applied", "reason": None},
            result_document=compact_result,
            binding=binding,
            current_record=record,
            accounting_version=PARTITIONED_ACTION_ACCOUNTING_VERSION,
            allow_v3_writer=False,
        )
        self.assertFalse(rep_v3.representable)
        self.assertEqual(rep_v3.refusal_code, "unsupported_v3_writer")

        # #431 / #436 observation objects are strictly rejected as writer/root authority:
        root_bytes = canonical_bytes(record.to_dict())
        archived_obs = ArchivedActionResultObservation(root_bytes, b"{}")
        ancestry_obs = ActionResultAncestryObservation(root_bytes, (archived_obs,))
        for obs in (archived_obs, ancestry_obs):
            rep_obs = evaluate_terminal_action_representability(
                identity=IDENTITY,
                intent_sha256="1" * 64,
                outcome={"kind": "applied", "reason": None},
                result_document=compact_result,
                binding=binding,
                current_record=obs,
            )
            self.assertFalse(rep_obs.representable)
            self.assertEqual(rep_obs.refusal_code, "observation_only_not_authority")

    def test_root_comment_and_request_budget_pre_effect_refusals(self) -> None:
        fixed_now = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)
        record = SessionRecord.create(IDENTITY, now=fixed_now)
        binding = self._default_binding()
        compact_result = asdict(
            MaintainerCommandResult(
                action="pause",
                applied=True,
                operator_paused=True,
                summary="paused",
            )
        )

        # 1. Request budget exhausted before effect:
        exhausted_pricing = ActionSettlementPricing(
            business_mutation_requests=3,
            refinalization_requests=3,
            receipt_publication_requests=2,
            terminal_close_requests=1,
            committed_requests=508,
            original_api_limit=512,
        )
        rep_budget = evaluate_terminal_action_representability(
            identity=IDENTITY,
            intent_sha256="1" * 64,
            outcome={"kind": "applied", "reason": None},
            result_document=compact_result,
            binding=binding,
            current_record=record,
            pricing=exhausted_pricing,
        )
        self.assertFalse(rep_budget.representable)
        self.assertEqual(
            rep_budget.refusal_code, "request_budget_exhausted_before_effect"
        )
        self.assertLess(rep_budget.pricing.remaining_headroom, 0)

        # 2. SessionRecord root reserve overflow (> 32,768B):
        base_bytes = estimate_session_record_bytes(record)
        rep_root = evaluate_terminal_action_representability(
            identity=IDENTITY,
            intent_sha256="1" * 64,
            outcome={"kind": "applied", "reason": None},
            result_document=compact_result,
            binding=binding,
            current_record=record,
            future_reserve_bytes=(MAX_SESSION_RECORD_BYTES - base_bytes + 64),
        )
        self.assertFalse(rep_root.representable)
        self.assertEqual(rep_root.refusal_code, "session_root_bytes_exceeded")
        self.assertGreater(rep_root.projected_record_bytes, MAX_SESSION_RECORD_BYTES)

        # 3. GitHub session comment overflow (> 65,536B via consumed_grants framing):
        many_grants = tuple(f"{i:064x}" for i in range(600))
        rep_comment = evaluate_terminal_action_representability(
            identity=IDENTITY,
            intent_sha256="1" * 64,
            outcome={"kind": "applied", "reason": None},
            result_document=compact_result,
            binding=binding,
            current_record=record,
            consumed_grants=many_grants,
        )
        self.assertFalse(rep_comment.representable)
        self.assertEqual(rep_comment.refusal_code, "session_root_bytes_exceeded")
        self.assertLessEqual(
            rep_comment.projected_record_bytes, MAX_SESSION_RECORD_BYTES
        )
        self.assertGreater(
            rep_comment.projected_comment_bytes, MAX_SESSION_COMMENT_BYTES
        )
        self.assertEqual(accounting_version_from_record(record), ACCOUNTING_VERSION)
        serialized = rep_comment.to_dict()
        self.assertFalse(serialized["representable"])
        self.assertFalse(serialized["authority"])

    def test_hosted_action_route_pre_effect_refusal_and_end_to_end_recovery(
        self,
    ) -> None:
        """Full synthetic ReviewResult -> SessionRecord -> action_run route + recovery."""

        with tempfile.TemporaryDirectory() as directory:
            f = _G34CommandFixture(directory, limit=512)
            patch_file = f.root / "review.patch"
            base_patch = patch_file.read_text(encoding="utf-8")
            # 38 binary files with 225-byte multisegment paths -> "file:<path>:unsupported:binary" = 249B each!
            binary_entries = "".join(
                f"\ndiff --git a/{p} b/{p}\nnew file mode 100644\n"
                f"index 0000000..1111111\nBinary files /dev/null and b/{p} differ\n"
                for p in (
                    f"services_{i:02d}/domain_{i:02d}/"
                    + _deterministic_valid_path(i, total_len=203)
                    for i in range(38)
                )
            )
            patch_file.write_text(
                base_patch + binary_entries, encoding="utf-8", newline="\n"
            )

            # 1. Run hosted partial review (38 skipped-binary files + 1 human finding)
            self.assertEqual(f.invoke(human=True), 0, f.error)
            review_doc = json.loads(
                (f.root / "review.json").read_text(encoding="utf-8")
            )
            self.assertEqual(review_doc["review_status"], "partial")
            before_cmd_record = parse_session_comment(
                f.comment["body"], identity=IDENTITY
            )
            assert before_cmd_record is not None
            index = parse_finding_index(
                f.reviews[-1]["body"], repository_id=123, pull_request=7, head_sha=HEAD
            )
            self.assertIsNotNone(index)
            assert index is not None
            finding_ref = "RS-" + index.entries[0].instance[:6].upper()
            extra_comments_before = len(f.extra_comments)

            # 2. Attempt accept-coverage-uncertainty on the 38-file G34 witness!
            f.SOURCE_ID = 301
            f.select_command(
                "@reviewsensei accept-coverage-uncertainty --reason accepted after full manual audit of the PR!!",
                new_run=True,
            )
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)

            # Prove ZERO business mutations, ZERO review PUTs, ZERO receipt comments!
            self.assertEqual(f.business_writes, 0)
            self.assertEqual(f.review_put_attempts, 0)
            self.assertEqual(f.review_updates, 0)
            receipt_comments = [
                c
                for c in f.extra_comments.values()
                if "reviewsensei:command-receipt:v1" in str(c.get("body", ""))
            ]
            self.assertEqual(receipt_comments, [])
            # Only the 1 archived predecessor accounting part was written on run rollover:
            self.assertEqual(len(f.extra_comments), extra_comments_before + 1)
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
            self.assertIn("carrier_inline_bound_exceeded", f.stdout)
            self.assertIn("@reviewsensei review remaining", f.stdout)

            refused_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert refused_record is not None
            self.assertEqual(refused_record.dispositions, ())
            refused_state = ApiRunAccounting.from_document(
                refused_record.api_run_accounting
            )
            refused_run_id = refused_state.identity.run_id
            self.assertEqual(refused_state.action["phase"], "terminal")
            self.assertEqual(
                refused_state.action["outcome"],
                {"kind": "explicit_noop", "reason": "no_change"},
            )
            retained_refusal = read_action_result(
                f.selected.ledger, IDENTITY, refused_record
            )
            self.assertFalse(retained_refusal["result"]["applied"])

            # Verify #431 current-root reader on live root:
            ctx = ActionResultReadContext(
                IDENTITY, f.selected.ledger.evidence_budget, max_depth=4
            )
            with patch.object(review_run, "physical_opener", f.external):
                current_obs = read_current_action_result(
                    f.selected.ledger, IDENTITY, context=ctx
                )
            self.assertEqual(current_obs["outcome"], refused_state.action["outcome"])

            # 3. UNIVERSAL RECOVERY ROUTE:
            # Because pre-effect refusal settled cleanly without wedging the PR,
            # the maintainer overrides the human finding AND narrows/completes the
            # remaining review scope within the representable envelope, then settles
            # coverage uncertainty and verifies to full native approval!
            f.SOURCE_ID = 302
            f.select_command(
                f"@reviewsensei override {finding_ref} acceptable risk",
                new_run=True,
            )
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            after_override = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert after_override is not None
            self.assertEqual(len(after_override.dispositions), 1)

            # Verify #436 bounded ancestry observation across the archived refusal run:
            ancestry_ctx = ActionResultReadContext(
                IDENTITY, f.selected.ledger.evidence_budget, max_depth=4
            )
            with patch.object(review_run, "physical_opener", f.external):
                ancestry = read_ancestor_action_results(
                    f.selected.ledger,
                    IDENTITY,
                    context=ancestry_ctx,
                    target_run_id=refused_run_id,
                )
            self.assertGreaterEqual(len(ancestry.ancestors), 1)
            assert ancestry.ancestors[0].result_document is not None
            archived_refusal_doc = json.loads(
                ancestry.ancestors[0].result_document.decode("utf-8")
            )
            self.assertFalse(archived_refusal_doc["result"]["applied"])

            # Push/update narrowed PR scope (20 uncovered binary files -> representable carrier <= 8,192B)
            f.past_jobs[f.run_id] = f.owner_job
            f.command_active = False
            f.owner_job = "hosted"
            f.event_name = "pull_request"
            f.next_run()
            bounded_head = "c" * 40
            f.live_head = f.requested_head = bounded_head
            bounded_binaries = "".join(
                f"\ndiff --git a/{p} b/{p}\nnew file mode 100644\n"
                f"index 0000000..1111111\nBinary files /dev/null and b/{p} differ\n"
                for p in (
                    f"services_{i:02d}/domain_{i:02d}/"
                    + _deterministic_valid_path(i, total_len=203)
                    for i in range(20)
                )
            )
            patch_file.write_text(
                base_patch + bounded_binaries, encoding="utf-8", newline="\n"
            )
            f.provider_comments = []
            self.assertEqual(f.invoke(human=False), 0, f.error)
            self.assertFalse(
                any(
                    r["state"] == "APPROVED" and r["commit_id"] == bounded_head
                    for r in f.reviews
                )
            )

            # Now accept-coverage-uncertainty fits comfortably inside 8,192B and settles applied=True!
            f.SOURCE_ID = 303
            f.select_command(
                "@reviewsensei accept-coverage-uncertainty --reason accepted bounded binary scope",
                new_run=True,
            )
            provider_before = f.provider_calls
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            self.assertEqual(f.provider_calls, provider_before)

            settled_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert settled_record is not None
            settled_state = ApiRunAccounting.from_document(
                settled_record.api_run_accounting
            )
            self.assertEqual(settled_state.action["phase"], "terminal")
            self.assertEqual(
                settled_state.action["outcome"],
                {"kind": "applied", "reason": None},
            )
            self.assertLessEqual(
                len(canonical_bytes(settled_state.action["result"])),
                MAX_MANIFEST_BYTES,
            )
            settled_action = read_action_result(
                f.selected.ledger, IDENTITY, settled_record
            )
            self.assertTrue(settled_action["result"]["applied"])
            projected = with_terminal_action_manifest(
                settled_state,
                manifest=settled_state.action["result"],
                outcome=settled_state.action["outcome"],
            )
            self.assertEqual(projected.action["phase"], "terminal")
            # Partial binary scope truthfully withholds native approval until scope is complete:
            self.assertFalse(
                any(
                    r["state"] == "APPROVED" and r["commit_id"] == bounded_head
                    for r in f.reviews
                )
            )

            # Complete analyzable scope on clean head, then converge via override + verify:
            f.past_jobs[f.run_id] = f.owner_job
            f.command_active = False
            f.owner_job = "hosted"
            f.event_name = "pull_request"
            f.next_run()
            clean_head = "d" * 40
            f.live_head = f.requested_head = clean_head
            patch_file.write_text(base_patch, encoding="utf-8", newline="\n")
            f.provider_comments = None
            self.assertEqual(f.invoke(human=True), 0, f.error)
            clean_index = parse_finding_index(
                f.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=clean_head,
            )
            assert clean_index is not None
            clean_ref = "RS-" + clean_index.entries[0].instance[:6].upper()

            f.SOURCE_ID = 304
            f.select_command(
                f"@reviewsensei override {clean_ref} acceptable risk",
                new_run=True,
            )
            provider_after_clean = f.provider_calls
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            self.assertEqual(f.provider_calls, provider_after_clean)
            self.assertEqual(
                sum(
                    r["state"] == "APPROVED" and r["commit_id"] == clean_head
                    for r in f.reviews
                ),
                1,
            )
            self.assertEqual(f.check_runs[-1]["conclusion"], "success")

            # Subsequent @reviewsensei verify re-confirms convergence with zero new inference:
            f.SOURCE_ID = 305
            f.select_command("@reviewsensei verify", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            self.assertEqual(f.provider_calls, provider_after_clean)
            self.assertEqual(
                sum(
                    r["state"] == "APPROVED" and r["commit_id"] == clean_head
                    for r in f.reviews
                ),
                1,
            )

    def test_validation_invariants_sandbox_snapshot_and_bridge_edges(self) -> None:
        from review_sensei.errors import ReviewInputError
        from review_sensei.provenance_manifest import prescribe_recovery
        from review_sensei.session import SessionIdentity
        from review_sensei.terminal_representability import (
            TerminalRepresentabilityReport,
            _InMemoryPreviewLedger,
            _prescribe_terminal_recovery,
        )

        fixed_now = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)
        record = SessionRecord.create(IDENTITY, now=fixed_now)
        binding = self._default_binding()
        compact_result = asdict(
            MaintainerCommandResult(
                action="pause",
                applied=True,
                operator_paused=True,
                summary="paused",
            )
        )

        # Missing repository_id on binding:
        with self.assertRaises(ReviewInputError):
            build_action_result_binding(
                identity=SessionIdentity("owner/repo", 7, None),
                policy_commit="a" * 40,
                head_sha="b" * 40,
                intent_sha256="1" * 64,
                configuration_digest="d" * 64,
                owner_generation=1,
                producer="github-bot:999",
            )

        # Pricing positive limit invariant:
        with self.assertRaises(ReviewInputError):
            ActionSettlementPricing(original_api_limit=0)

        # Report invariants:
        pricing = ActionSettlementPricing()
        rec_presc = prescribe_recovery("supported_read_only_observation")
        with self.assertRaises(ReviewInputError):
            TerminalRepresentabilityReport(
                representable=True,
                refusal_code="carrier_inline_bound_exceeded",
                reason="bad",
                disposition_entry_count=0,
                max_disposition_token_bytes=0,
                disposition_bytes=0,
                envelope_bytes=0,
                carrier_bytes=0,
                payload_bytes=0,
                manifest_bytes=100,
                projected_record_bytes=100,
                projected_comment_bytes=100,
                future_reserve_bytes=0,
                accounting_version=ACCOUNTING_VERSION,
                v3_writer_enabled=False,
                pricing=pricing,
                recovery=rec_presc,
            )
        with self.assertRaises(ReviewInputError):
            TerminalRepresentabilityReport(
                representable=False,
                refusal_code="unknown_code",
                reason="bad",
                disposition_entry_count=0,
                max_disposition_token_bytes=0,
                disposition_bytes=0,
                envelope_bytes=0,
                carrier_bytes=0,
                payload_bytes=0,
                manifest_bytes=None,
                projected_record_bytes=0,
                projected_comment_bytes=0,
                future_reserve_bytes=0,
                accounting_version=ACCOUNTING_VERSION,
                v3_writer_enabled=False,
                pricing=pricing,
                recovery=rec_presc,
            )

        # Non-coverage batching prescription:
        override_rx = _prescribe_terminal_recovery(
            "carrier_inline_bound_exceeded", action_name="accept-risk"
        )
        self.assertEqual(override_rx.action, "verify_convergence")

        # Unsupported accounting version & current_record=None path:
        with self.assertRaises(ReviewInputError):
            evaluate_terminal_action_representability(
                identity=IDENTITY,
                intent_sha256="1" * 64,
                outcome={"kind": "applied", "reason": None},
                result_document=compact_result,
                binding=binding,
                current_record=None,
                accounting_version="caller-requests-v99",
            )
        rep_no_rec = evaluate_terminal_action_representability(
            identity=IDENTITY,
            intent_sha256="1" * 64,
            outcome={"kind": "applied", "reason": None},
            result_document=compact_result,
            binding=binding,
            current_record=None,
        )
        self.assertTrue(rep_no_rec.representable)
        self.assertEqual(rep_no_rec.projected_record_bytes, 0)

        # Sandbox ledger snapshot writes + missing-record initialize/reenroll/abandoned:
        sandbox = _InMemoryPreviewLedger(None, enable_disposition_snapshot_writes=True)
        self.assertEqual(sandbox.load(IDENTITY).status, "missing")
        with self.assertRaises(ReviewInputError):
            sandbox.replace(IDENTITY, lambda r: r)
        with self.assertRaises(ReviewInputError):
            sandbox.continue_after_abandoned_analysis(IDENTITY)
        with self.assertRaises(ReviewInputError):
            sandbox._read_part(IDENTITY, "missing-part")
        sandbox.initialize(IDENTITY, now=fixed_now)
        sandbox.continue_after_abandoned_analysis(IDENTITY, now=fixed_now)
        sandbox.reenroll(IDENTITY, now=fixed_now)
        sandbox.initialize_with_mutation(IDENTITY, lambda r: r, now=fixed_now)
        sid = sandbox._write_part(IDENTITY, "p1", {"ok": True})
        self.assertEqual(sandbox._read_part(IDENTITY, sid).document, {"ok": True})

        # Preview with current_record=None on 20 files exercises initialize_with_mutation:
        files_20 = tuple(
            FileCoverage(
                path=_deterministic_valid_path(i, total_len=227),
                outcome="budget-exhausted",
            )
            for i in range(20)
        )
        inc_20 = incomplete_coverage_from_result(
            ReviewResult(
                summary="Partial",
                comments=(),
                provider="openai",
                review_status="partial",
                coverage=CoverageManifest(
                    files=files_20,
                    enumerated_paths=tuple(f.path for f in files_20),
                ),
            )
        )
        _, snap_rec, snap_rep = preview_session_command_representability(
            identity=IDENTITY,
            current_record=None,
            command=MaintainerCommand(
                action="accept-coverage-uncertainty",
                actor="alice",
                reason="snapshot preview",
                head_sha="b" * 40,
            ),
            intent_sha256="1" * 64,
            binding=binding,
            actor_role="OWNER",
            policy_digest="c" * 64,
            provenance="comment:99",
            incomplete_coverage=inc_20,
            enable_disposition_snapshot_writes=False,
            evidence_budget=EvidenceReadBudget(),
            now=fixed_now,
        )
        self.assertIsNotNone(snap_rec)
        self.assertTrue(snap_rep.representable)
        self.assertIsNone(snap_rep.refusal_code)

        # Bridge edge cases (run=None, missing _effect_record, fallback boundary, exhausted noop):
        self.assertIsNone(
            guard_current_command_representability(
                current_action=SimpleNamespace(run=None),
                ledger=sandbox,
                identity=IDENTITY,
                command=MaintainerCommand(action="pause", actor="alice"),
                head_sha="b" * 40,
                writes_enabled=True,
            )
        )
        fake_caller_no_rec = SimpleNamespace(_effect_record=None)
        self.assertIsNone(
            guard_current_command_representability(
                current_action=SimpleNamespace(
                    run=SimpleNamespace(caller=fake_caller_no_rec)
                ),
                ledger=sandbox,
                identity=IDENTITY,
                command=MaintainerCommand(action="pause", actor="alice"),
                head_sha="b" * 40,
                writes_enabled=True,
            )
        )
        # Exhausted noop raises TerminalRepresentabilityError before any write:
        fake_accounting = SimpleNamespace(
            version=ACCOUNTING_VERSION,
            action=None,
            configuration=SimpleNamespace(limit=5, commit="a" * 40, digest="d" * 64),
            owner=SimpleNamespace(generation=1),
            blocks=(),
        )
        exhausted_caller = SimpleNamespace(
            accounting=fake_accounting,
            usable=0,
            _block="b0",
            _effect_record=record,
            _action_effect_boundary=SimpleNamespace(intent_sha256="2" * 64),
            budget_view=None,
        )
        with self.assertRaises(TerminalRepresentabilityError):
            guard_current_command_representability(
                current_action=SimpleNamespace(
                    run=SimpleNamespace(caller=exhausted_caller)
                ),
                ledger=sandbox,
                identity=IDENTITY,
                command=MaintainerCommand(action="pause", actor="alice"),
                head_sha="b" * 40,
                writes_enabled=True,
            )

        # with_terminal_action_manifest error when action is None:
        from tests.test_api_work import accounting as make_accounting

        base_acc = make_accounting()
        rec_with_acc = record.evolve(api_run_accounting=base_acc.to_document())
        self.assertEqual(accounting_version_from_record(rec_with_acc), base_acc.version)
        with self.assertRaises(ReviewInputError):
            with_terminal_action_manifest(
                base_acc, manifest={}, outcome={"kind": "applied", "reason": None}
            )

    def test_preview_snapshot_staging_preserves_shared_budget_without_new_budget_init(
        self,
    ) -> None:
        from review_sensei.disposition import FindingDisposition, FindingIdentity
        from review_sensei.errors import ReviewInputError
        from review_sensei.models import ReviewTransaction

        head_sha = "b" * 40
        tx_id = ReviewTransaction.derive_id(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha="a" * 40,
            head_sha=head_sha,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            evidence_digest="e" * 64,
            reservation_id="2" * 64,
        )
        tx = ReviewTransaction(
            transaction_id=tx_id,
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha="a" * 40,
            head_sha=head_sha,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            evidence_digest="e" * 64,
            generation=1,
            reservation_id="2" * 64,
            phase="publication_succeeded",
            result_sha256="f" * 64,
        )
        prior_dispositions = tuple(
            FindingDisposition(
                fingerprint=f"{i:064x}",
                action="accept-risk",
                reason="ok",
                actor="alice",
                head_sha=head_sha,
                actor_role="OWNER",
                finding_reference=f"RS-{i:06X}",
                recorded_at="2026-11-11T00:00:00Z",
                policy_digest="c" * 64,
                provenance=f"comment:{i + 1}",
                target_kind="finding",
            ).to_dict()
            for i in range(20)
        )
        record = SessionRecord.create(IDENTITY).evolve(
            completed_initial_reviews=1,
            last_committed_reservation_id="2" * 64,
            transaction=tx,
            dispositions=prior_dispositions,
        )
        inventory = tuple(
            FindingIdentity(
                instance=f"{20 + j:06x}{'a' * 58}",
                needs_human=True,
            )
            for j in range(5)
        )
        refs = tuple(f"RS-{20 + j:06X}" for j in range(5))
        cmd = MaintainerCommand(
            action="accept-risk",
            actor="alice",
            reason="acceptable risk",
            head_sha=head_sha,
            finding_references=refs,
        )
        shared_budget = EvidenceReadBudget()
        binding = self._default_binding()
        with patch.object(
            EvidenceReadBudget,
            "__init__",
            side_effect=AssertionError("no replacement phase budget"),
        ):
            prev_res, prev_rec, rep = preview_session_command_representability(
                identity=IDENTITY,
                current_record=record,
                command=cmd,
                intent_sha256="1" * 64,
                binding=binding,
                finding_inventory=inventory,
                actor_role="OWNER",
                policy_digest="c" * 64,
                provenance="comment:999",
                enable_disposition_snapshot_writes=True,
                evidence_budget=shared_budget,
            )
            assert prev_res is not None and prev_rec is not None
            self.assertTrue(prev_res.applied)
            self.assertIsNotNone(prev_rec.disposition_snapshot)
            self.assertTrue(rep.representable)
            self.assertEqual(shared_budget.calls, 0)

            # Follow-up verify with pre-loaded snapshot entries serves them in-memory with 0 budget calls:
            new_disps = tuple(item.to_dict() for item in prev_res.dispositions)
            all_25_disps = prior_dispositions + new_disps
            verify_res, verify_rec, verify_rep = (
                preview_session_command_representability(
                    identity=IDENTITY,
                    current_record=prev_rec,
                    command=MaintainerCommand(
                        action="verify", actor="alice", head_sha=head_sha
                    ),
                    intent_sha256="1" * 64,
                    binding=binding,
                    enable_disposition_snapshot_writes=True,
                    evidence_budget=shared_budget,
                    preloaded_dispositions=all_25_disps,
                )
            )
            assert verify_res is not None and verify_rec is not None
            self.assertTrue(verify_res.applied)
            self.assertEqual(
                verify_rec.disposition_snapshot, prev_rec.disposition_snapshot
            )
            self.assertTrue(verify_rep.representable)
            self.assertGreaterEqual(
                verify_rep.projected_record_bytes,
                estimate_session_record_bytes(prev_rec),
            )
            self.assertEqual(shared_budget.calls, 0)

            # Calling verify on an external partitioned snapshot WITHOUT preloaded parts refuses conservatively:
            _, _, unbacked_rep = preview_session_command_representability(
                identity=IDENTITY,
                current_record=prev_rec,
                command=MaintainerCommand(
                    action="verify", actor="alice", head_sha=head_sha
                ),
                intent_sha256="1" * 64,
                binding=binding,
                enable_disposition_snapshot_writes=True,
                evidence_budget=shared_budget,
            )
            self.assertFalse(unbacked_rep.representable)
            self.assertEqual(
                unbacked_rep.refusal_code, "snapshot_parts_unavailable_for_preview"
            )
            self.assertEqual(shared_budget.calls, 0)

            # Also cover >24 KiB partitioned in-memory staging + readback without budget calls:
            from review_sensei.terminal_representability import _InMemoryPreviewLedger

            preview_ledger = _InMemoryPreviewLedger(
                record,
                enable_disposition_snapshot_writes=True,
                evidence_budget=shared_budget,
            )
            large_doc = {
                "schema_version": "1.0",
                "dispositions": [
                    {
                        "fingerprint": f"{k:064x}",
                        "reason": "r" * 480,
                    }
                    for k in range(60)
                ],
            }
            part_binding = {
                **binding,
                "purpose": "dispositions",
                "producer": "local-ledger",
                "schema_version": "1.0",
            }
            staged_manifest = preview_ledger.stage_evidence(
                IDENTITY,
                binding=part_binding,
                document=large_doc,
                item_count=60,
                max_manifest_bytes=8192,
            )
            self.assertEqual(staged_manifest["encoding"], "partitioned-json-v1")
            roundtrip = preview_ledger.read_evidence(
                IDENTITY,
                staged_manifest,
                expected_binding=part_binding,
            )
            self.assertEqual(roundtrip, large_doc)
            self.assertEqual(shared_budget.calls, 0)
            with self.assertRaises(ReviewInputError):
                preview_ledger.read_evidence(
                    IDENTITY,
                    staged_manifest,
                    expected_binding={**part_binding, "generation": 99},
                )
            with self.assertRaisesRegex(
                ReviewInputError, "unsupported preview partition purpose"
            ):
                preview_ledger.stage_evidence(
                    IDENTITY,
                    binding={**part_binding, "purpose": "results"},
                    document=large_doc,
                    item_count=60,
                    max_manifest_bytes=8192,
                )

    def test_review_findings_regressions_parity_binding_reserve_and_pre_receipt_guard(
        self,
    ) -> None:
        from review_sensei_github.action_representability import (
            _command_pricing,
            require_action_settlement_representable,
        )
        from review_sensei_github.api_run_host import review_startup_plan
        from review_sensei_github.session_ledger import render_session_comment

        from review_sensei.api_work import (
            ApiExecutionOwner,
            ApiRunIdentity,
            FrozenApiConfiguration,
            initial_accounting,
        )
        from review_sensei.errors import ReviewInputError
        from review_sensei.session import SessionIdentity
        from tests.test_quarantine_lineage import lineage_value

        # RS-B946F3: lockstep byte-for-byte parity with production render_session_comment (v1 + v2 + grants).
        v1_rec = SessionRecord.create(IDENTITY)
        v2_id = SessionIdentity("owner/repo", 7, repository_id=123)
        v2_state = initial_accounting(
            ApiRunIdentity(123, 900),
            FrozenApiConfiguration("c" * 40, "d" * 64, 512),
            ApiExecutionOwner(1, "hosted", 0),
            review_startup_plan("hosted"),
            prior_nonstartup_liability_upper_bound=0,
        )
        v2_rec = SessionRecord.create(
            v2_id,
            generation=10,
            api_run_accounting=v2_state.to_document(),
            quarantine_lineage=lineage_value(),
        )
        grants = ("1" * 64, "2" * 64, "3" * 64)
        for rec_id, rec in ((IDENTITY, v1_rec), (v2_id, v2_rec)):
            for grant_tuple in ((), grants):
                rendered = render_session_comment(
                    repository_id=rec_id.repository_id or 1,
                    pull_request=rec_id.pull_request,
                    record=rec,
                    consumed_grants=grant_tuple,
                )
                self.assertEqual(
                    estimate_session_comment_bytes(
                        rec,
                        repository_id=rec_id.repository_id or 1,
                        pull_request=rec_id.pull_request,
                        consumed_grants=grant_tuple,
                    ),
                    len(rendered.encode("utf-8")),
                )

        # RS-768225 & RS-0C90BC: carrier fits 8192B alone, yet outer inline_manifest (with binding) exceeds 8192B!
        import base64
        import hashlib

        binding = self._default_binding()
        near_carrier_summary = base64.b64encode(
            b"".join(
                hashlib.sha256(f"blk-{idx}".encode("ascii")).digest()
                for idx in range(178)
            )
        ).decode("ascii")
        near_report = evaluate_terminal_action_representability(
            identity=IDENTITY,
            intent_sha256="1" * 64,
            outcome={"kind": "applied", "reason": None},
            result_document={"summary": near_carrier_summary},
            binding=binding,
            current_record=v1_rec,
        )
        self.assertLessEqual(near_report.carrier_bytes, 8192)
        self.assertIsNone(near_report.manifest_bytes)
        self.assertFalse(near_report.representable)
        self.assertEqual(near_report.refusal_code, "carrier_inline_bound_exceeded")

        # RS-2217B7 & RS-BDA89A: non-matching ReviewInputError (even containing 'disposition' and 'limit') propagates!
        with patch(
            "review_sensei.terminal_representability.apply_session_command",
            side_effect=ReviewInputError("maintainer disposition reason limit invalid"),
        ):
            with self.assertRaisesRegex(
                ReviewInputError, "maintainer disposition reason limit invalid"
            ):
                preview_session_command_representability(
                    identity=IDENTITY,
                    current_record=v1_rec,
                    command=MaintainerCommand(action="pause", actor="alice"),
                    intent_sha256="1" * 64,
                    binding=binding,
                )

        # RS-C6F5B8: null preview_result fails closed.
        with patch(
            "review_sensei.terminal_representability.apply_session_command",
            return_value=(v1_rec, None),
        ):
            with self.assertRaisesRegex(
                ReviewInputError, "preview session command returned no result"
            ):
                preview_session_command_representability(
                    identity=IDENTITY,
                    current_record=v1_rec,
                    command=MaintainerCommand(action="pause", actor="alice"),
                    intent_sha256="1" * 64,
                    binding=binding,
                )

        # RS-28E4D8 & RS-BDA89A: replacement disposition_snapshot and reenroll do NOT double-count existing_snapshot_bytes!
        from review_sensei.bounded_evidence import EvidenceReadBudget
        from review_sensei.disposition import FindingDisposition, FindingIdentity
        from review_sensei.models import ReviewTransaction

        head_sha = "b" * 40
        tx_id = ReviewTransaction.derive_id(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha="a" * 40,
            head_sha=head_sha,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            evidence_digest="e" * 64,
            reservation_id="2" * 64,
        )
        tx = ReviewTransaction(
            transaction_id=tx_id,
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha="a" * 40,
            head_sha=head_sha,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            evidence_digest="e" * 64,
            generation=1,
            reservation_id="2" * 64,
            phase="publication_succeeded",
            result_sha256="f" * 64,
        )
        disps_14 = tuple(
            FindingDisposition(
                fingerprint=f"{i:064x}",
                action="accept-risk",
                reason="x" * 220,
                actor="alice",
                head_sha=head_sha,
                actor_role="OWNER",
                finding_reference=f"RS-{i:06X}",
                recorded_at="2026-11-11T00:00:00Z",
                policy_digest="c" * 64,
                provenance=f"comment:{i + 1}",
                target_kind="finding",
            ).to_dict()
            for i in range(14)
        )
        snap_base = v1_rec.evolve(
            completed_initial_reviews=1,
            last_committed_reservation_id="2" * 64,
            transaction=tx,
            dispositions=disps_14,
        )
        with tempfile.TemporaryDirectory() as snap_tmp:
            snap_ledger = LocalSessionLedger(
                Path(snap_tmp),
                enable_disposition_snapshot_writes=True,
                evidence_budget=EvidenceReadBudget(),
            )
            snap_ledger.initialize(IDENTITY)
            snap_ledger.replace(IDENTITY, lambda _r: snap_base)
            snap_rec_1_real, _ = apply_session_command(
                snap_ledger,
                IDENTITY,
                MaintainerCommand(
                    action="accept-risk",
                    actor="alice",
                    reason="x" * 220,
                    head_sha=head_sha,
                    finding_references=("RS-000099",),
                ),
                finding_inventory=(
                    FindingIdentity(instance=f"000099{'a' * 58}", needs_human=True),
                ),
                actor_role="OWNER",
                policy_digest="c" * 64,
                provenance="comment:100",
            )
            _, snap_rec_1, snap_rep_1 = preview_session_command_representability(
                identity=IDENTITY,
                current_record=snap_base,
                command=MaintainerCommand(
                    action="accept-risk",
                    actor="alice",
                    reason="x" * 220,
                    head_sha=head_sha,
                    finding_references=("RS-000099",),
                ),
                intent_sha256="1" * 64,
                binding=binding,
                finding_inventory=(
                    FindingIdentity(instance=f"000099{'a' * 58}", needs_human=True),
                ),
                actor_role="OWNER",
                policy_digest="c" * 64,
                provenance="comment:100",
                enable_disposition_snapshot_writes=True,
                evidence_budget=EvidenceReadBudget(),
            )
            assert (
                snap_rec_1 is not None and snap_rec_1.disposition_snapshot is not None
            )
            self.assertEqual(snap_rep_1.future_reserve_bytes, 0)

            # Non-snapshot command on already-snapshotted record preserves disposition_snapshot directly on pause_rec:
            _, pause_rec, pause_rep = preview_session_command_representability(
                identity=IDENTITY,
                current_record=snap_rec_1_real,
                command=MaintainerCommand(action="pause", actor="alice"),
                intent_sha256="1" * 64,
                binding=binding,
                actor_role="OWNER",
            )
            assert pause_rec is not None
            self.assertEqual(
                pause_rec.disposition_snapshot, snap_rec_1_real.disposition_snapshot
            )
            self.assertEqual(
                pause_rep.projected_record_bytes,
                estimate_session_record_bytes(pause_rec),
            )
            self.assertGreater(pause_rep.projected_record_bytes, 2_000)

            # Command staging a replacement snapshot (> 10,240B) does NOT double-count existing_snapshot_bytes:
            big_cov = tuple(
                f"file:{'p' * 220}:{idx:03d}:budget-exhausted" for idx in range(44)
            )
            _, snap_rec_2, snap_rep_2 = preview_session_command_representability(
                identity=IDENTITY,
                current_record=snap_rec_1_real,
                command=MaintainerCommand(
                    action="accept-coverage-uncertainty",
                    actor="alice",
                    reason="ok",
                    head_sha=head_sha,
                ),
                intent_sha256="1" * 64,
                binding=binding,
                actor_role="OWNER",
                policy_digest="c" * 64,
                provenance="comment:101",
                incomplete_coverage=big_cov,
                enable_disposition_snapshot_writes=True,
                evidence_budget=EvidenceReadBudget(),
                source_ledger=snap_ledger,
            )
            assert (
                snap_rec_2 is not None and snap_rec_2.disposition_snapshot is not None
            )
            self.assertEqual(snap_rep_2.future_reserve_bytes, 0)

            # Reenroll on snapshotted record clears snapshot -> reserve is 0 (not inflated by old snapshot):
            _, reenrolled_rec, reenroll_rep = preview_session_command_representability(
                identity=IDENTITY,
                current_record=snap_rec_1_real,
                command=MaintainerCommand(action="reenroll", actor="alice"),
                intent_sha256="1" * 64,
                binding=binding,
                actor_role="OWNER",
            )
            assert reenrolled_rec is not None
            self.assertIsNone(reenrolled_rec.disposition_snapshot)
            self.assertEqual(reenroll_rep.future_reserve_bytes, 0)

        # RS-D8093A: missing caller action intent digest fails closed instead of '0' * 64 placeholder:
        missing_intent_caller = SimpleNamespace(
            accounting=SimpleNamespace(
                version=ACCOUNTING_VERSION,
                action=None,
                configuration=SimpleNamespace(
                    limit=64, commit="a" * 40, digest="d" * 64
                ),
                owner=SimpleNamespace(generation=1),
                blocks=(),
            ),
            usable=32,
            _block="b0",
            _effect_record=v1_rec,
            _action_effect_boundary=None,
            budget_view=None,
        )
        with self.assertRaisesRegex(
            ReviewInputError, "caller action intent digest is unavailable"
        ):
            guard_current_command_representability(
                current_action=SimpleNamespace(
                    run=SimpleNamespace(caller=missing_intent_caller)
                ),
                ledger=SimpleNamespace(evidence_producer=lambda: "local-ledger"),
                identity=IDENTITY,
                command=MaintainerCommand(action="pause", actor="alice"),
                head_sha=head_sha,
                writes_enabled=True,
            )

        # RS-9C295F, RS-38FBAE, RS-F4E30D: receipt pricing on accept-coverage-uncertainty / pause and pre-receipt settlement guard:
        valid_caller = SimpleNamespace(
            accounting=SimpleNamespace(
                version=ACCOUNTING_VERSION,
                action=None,
                configuration=SimpleNamespace(
                    limit=64, commit="a" * 40, digest="d" * 64
                ),
                owner=SimpleNamespace(generation=1),
                blocks=(),
            ),
            usable=50,
            _block="b0",
            _effect_record=v1_rec,
            _action_effect_boundary=SimpleNamespace(intent_sha256="3" * 64),
            budget_view=None,
        )
        cov_pricing = _command_pricing(
            valid_caller, action="accept-coverage-uncertainty", writes_enabled=True
        )
        self.assertEqual(cov_pricing.receipt_publication_requests, 2)
        pause_pricing = _command_pricing(
            valid_caller, action="pause", writes_enabled=True
        )
        self.assertEqual(pause_pricing.receipt_publication_requests, 2)

        fake_run = SimpleNamespace(
            caller=valid_caller,
            ledger=SimpleNamespace(evidence_producer=lambda: "local-ledger"),
            identity=IDENTITY,
            head_sha=head_sha,
        )
        oversized_summary = base64.b64encode(
            b"".join(
                hashlib.sha256(f"ov-{idx}".encode("ascii")).digest()
                for idx in range(260)
            )
        ).decode("ascii")
        with self.assertRaises(TerminalRepresentabilityError) as settle_ctx:
            require_action_settlement_representable(
                fake_run,
                outcome={"kind": "applied", "reason": None},
                result_document={"summary": oversized_summary},
                extra_work_requests=2,
                action_name="accept-coverage-uncertainty",
            )
        self.assertEqual(
            settle_ctx.exception.report.refusal_code, "carrier_inline_bound_exceeded"
        )

    def test_two_decision_disposition_snapshot_already_recorded_parity_and_zero_budget_calls(
        self,
    ) -> None:
        """Exact G34 2-decision disposition_snapshot reproducer and pre-loaded snapshot parity.

        On unpatched 07179c0f, preview stripped current_record.disposition_snapshot=None
        and reported carrier_bytes=809, payload_bytes=754, envelope_bytes=1,439,
        manifest_bytes=1,382, representable=True, receipt='recorded', snapshot=None,
        whereas actual apply_session_command retained disposition_snapshot and produced
        carrier_bytes=9,354 (> 8,192), payload_bytes=9,299, envelope_bytes=10,866,
        manifest_bytes=None, representable=False ('carrier_inline_bound_exceeded'),
        receipt='already-recorded' (via stored[-1]).
        """
        from pathlib import Path

        from review_sensei.disposition import (
            FindingIdentity,
            apply_session_command,
            session_dispositions,
        )
        from review_sensei.models import ReviewTransaction
        from review_sensei.session import LocalSessionLedger, read_session_dispositions
        from review_sensei.terminal_representability import _InMemoryPreviewLedger

        head_sha = "b" * 40
        fixed_now = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)
        intent_sha = action_intent_sha256({"purpose": "command", "source_id": 42})
        binding = build_action_result_binding(
            identity=IDENTITY,
            policy_commit="a" * 40,
            head_sha=head_sha,
            intent_sha256=intent_sha,
            configuration_digest="d" * 64,
            owner_generation=1,
            producer="local-ledger",
        )
        finding = FindingIdentity(instance="abcdef" + "1" * 58, needs_human=True)
        tx_id = ReviewTransaction.derive_id(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha="a" * 40,
            head_sha=head_sha,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            evidence_digest="e" * 64,
            reservation_id="2" * 64,
        )
        tx = ReviewTransaction(
            transaction_id=tx_id,
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha="a" * 40,
            head_sha=head_sha,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            evidence_digest="e" * 64,
            generation=1,
            reservation_id="2" * 64,
            phase="publication_succeeded",
            result_sha256="f" * 64,
        )

        for file_count, expected_carrier, expected_payload, expected_envelope in (
            (37, 9_354, 9_299, 10_866),
            (38, 9_578, 9_523, 11_118),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                ledger = LocalSessionLedger(
                    Path(tmp),
                    enable_disposition_snapshot_writes=True,
                    evidence_budget=EvidenceReadBudget(),
                )
                rec = ledger.initialize(IDENTITY, now=fixed_now)
                rec = ledger.replace(
                    IDENTITY,
                    lambda r: r.evolve(
                        completed_initial_reviews=1,
                        last_committed_reservation_id="2" * 64,
                        transaction=tx,
                    ),
                    now=fixed_now,
                )
                files = tuple(
                    FileCoverage(
                        path=_deterministic_valid_path(i, total_len=227),
                        outcome="budget-exhausted",
                    )
                    for i in range(file_count)
                )
                partial_result = ReviewResult(
                    summary="Partial",
                    comments=(),
                    provider="openai",
                    review_status="partial",
                    coverage=CoverageManifest(
                        files=files,
                        enumerated_paths=tuple(f.path for f in files),
                    ),
                )
                incomplete = incomplete_coverage_from_result(partial_result)
                self.assertEqual(len(incomplete), file_count + 1)
                self.assertEqual(
                    sum(1 for tok in incomplete if tok.startswith("result:")), 1
                )

                cmd_override = MaintainerCommand(
                    action="accept-risk",
                    actor="alice",
                    reason="ok",
                    head_sha=head_sha,
                    finding_references=("RS-ABCDEF",),
                )
                rec, first_res = apply_session_command(
                    ledger,
                    IDENTITY,
                    cmd_override,
                    finding_inventory=(finding,),
                    actor_role="OWNER",
                    policy_digest="c" * 64,
                    provenance="comment:41",
                    now=fixed_now,
                )
                self.assertEqual(first_res.receipts[0].outcome, "recorded")
                self.assertIsNone(rec.disposition_snapshot)

                cmd_cov = MaintainerCommand(
                    action="accept-coverage-uncertainty",
                    actor="alice",
                    reason="ok",
                    head_sha=head_sha,
                )
                rec, _ = apply_session_command(
                    ledger,
                    IDENTITY,
                    cmd_cov,
                    actor_role="OWNER",
                    policy_digest="c" * 64,
                    provenance="comment:42",
                    incomplete_coverage=incomplete,
                    now=fixed_now,
                )
                self.assertIsNotNone(rec.disposition_snapshot)
                assert rec.disposition_snapshot is not None
                rec_manifest = rec.disposition_snapshot["state_manifest"]
                assert isinstance(rec_manifest, dict)
                self.assertEqual(rec_manifest["encoding"], "partitioned-json-v1")
                self.assertEqual(len(session_dispositions(rec, ledger=ledger)), 2)

                shared_budget = EvidenceReadBudget()
                prev_res, prev_rec, prev_rep = preview_session_command_representability(
                    identity=IDENTITY,
                    current_record=rec,
                    command=cmd_override,
                    intent_sha256=intent_sha,
                    binding=binding,
                    finding_inventory=(finding,),
                    actor_role="OWNER",
                    policy_digest="c" * 64,
                    provenance="comment:43",
                    enable_disposition_snapshot_writes=True,
                    evidence_budget=shared_budget,
                    source_ledger=ledger,
                    now=fixed_now,
                )
                self.assertEqual(shared_budget.calls, 0)
                assert prev_res is not None and prev_rec is not None
                self.assertEqual(prev_res.receipts[0].outcome, "already-recorded")
                self.assertEqual(
                    prev_rec.disposition_snapshot, rec.disposition_snapshot
                )

                real_rec, real_res = apply_session_command(
                    ledger,
                    IDENTITY,
                    cmd_override,
                    finding_inventory=(finding,),
                    actor_role="OWNER",
                    policy_digest="c" * 64,
                    provenance="comment:43",
                    now=fixed_now,
                )
                real_rep = evaluate_terminal_action_representability(
                    identity=IDENTITY,
                    intent_sha256=intent_sha,
                    outcome={"kind": "applied", "reason": None},
                    result_document=asdict(real_res),
                    binding=binding,
                    current_record=real_rec,
                    resolved_dispositions=read_session_dispositions(ledger, real_rec),
                )
                self.assertEqual(real_res.receipts[0].outcome, "already-recorded")
                self.assertEqual(asdict(prev_res), asdict(real_res))
                self.assertEqual(prev_rep.carrier_bytes, expected_carrier)
                self.assertEqual(prev_rep.payload_bytes, expected_payload)
                self.assertEqual(prev_rep.envelope_bytes, expected_envelope)
                self.assertEqual(prev_rep.carrier_bytes, real_rep.carrier_bytes)
                self.assertEqual(prev_rep.disposition_bytes, real_rep.disposition_bytes)
                self.assertGreater(prev_rep.disposition_bytes, MAX_DISPOSITION_BYTES)
                self.assertIsNone(prev_rep.manifest_bytes)
                self.assertFalse(prev_rep.representable)
                self.assertEqual(prev_rep.refusal_code, "carrier_inline_bound_exceeded")

                # Also verify _InMemoryPreviewLedger.read_disposition_snapshot via preloaded_dispositions & tamper guards:
                auth_disps = read_session_dispositions(ledger, rec)
                mem_ledger = _InMemoryPreviewLedger(
                    rec,
                    producer="local-ledger",
                    enable_disposition_snapshot_writes=True,
                    evidence_budget=shared_budget,
                    preloaded_dispositions=auth_disps,
                )
                loaded_via_method = mem_ledger.read_disposition_snapshot(IDENTITY)
                self.assertEqual(len(loaded_via_method), 2)
                self.assertEqual(shared_budget.calls, 0)

                tampered = (
                    {**dict(auth_disps[0]), "reason": "tampered"},
                    dict(auth_disps[1]),
                )
                with self.assertRaisesRegex(
                    ReviewInputError, "digest does not match authenticated root"
                ):
                    _InMemoryPreviewLedger(
                        rec,
                        producer="local-ledger",
                        preloaded_dispositions=tampered,
                    )
                with self.assertRaisesRegex(
                    ReviewInputError, "item_count does not match authenticated root"
                ):
                    _InMemoryPreviewLedger(
                        rec,
                        producer="local-ledger",
                        preloaded_dispositions=(dict(auth_disps[0]),),
                    )

        # Also cover v2 paged disposition_snapshot (supports_paged_disposition_snapshot_writes=True) in preview:
        class _PagedLocalLedger(LocalSessionLedger):
            @property
            def supports_paged_disposition_snapshot_writes(self) -> bool:
                return True

        with tempfile.TemporaryDirectory() as paged_tmp:
            paged_ledger = _PagedLocalLedger(
                Path(paged_tmp),
                enable_disposition_snapshot_writes=True,
                evidence_budget=EvidenceReadBudget(),
            )
            p_rec = paged_ledger.initialize(IDENTITY, now=fixed_now)
            p_rec = paged_ledger.replace(
                IDENTITY,
                lambda r: r.evolve(
                    completed_initial_reviews=1,
                    last_committed_reservation_id="2" * 64,
                    transaction=tx,
                ),
                now=fixed_now,
            )
            inv_10 = tuple(
                FindingIdentity(instance=f"{k:06x}{'a' * 58}", needs_human=True)
                for k in range(10)
            )
            refs_10 = tuple(f"RS-{k:06X}" for k in range(10))
            p_rec, _ = apply_session_command(
                paged_ledger,
                IDENTITY,
                MaintainerCommand(
                    action="accept-risk",
                    actor="alice",
                    reason="batch 1",
                    head_sha=head_sha,
                    finding_references=refs_10[:9],
                ),
                finding_inventory=inv_10,
                actor_role="OWNER",
                policy_digest="c" * 64,
                provenance="comment:50",
                now=fixed_now,
            )
            assert p_rec.disposition_snapshot is not None
            self.assertEqual(p_rec.disposition_snapshot["schema_version"], "2.0")

            paged_budget = EvidenceReadBudget()
            # Append 10th finding onto v2 paged snapshot in preview:
            app_res, app_rec, app_rep = preview_session_command_representability(
                identity=IDENTITY,
                current_record=p_rec,
                command=MaintainerCommand(
                    action="accept-risk",
                    actor="alice",
                    reason="batch 2",
                    head_sha=head_sha,
                    finding_references=(refs_10[9],),
                ),
                intent_sha256=intent_sha,
                binding=binding,
                finding_inventory=inv_10,
                actor_role="OWNER",
                policy_digest="c" * 64,
                provenance="comment:51",
                enable_disposition_snapshot_writes=True,
                evidence_budget=paged_budget,
                source_ledger=paged_ledger,
                now=fixed_now,
            )
            self.assertEqual(paged_budget.calls, 0)
            assert app_res is not None and app_rec is not None
            self.assertEqual(app_res.receipts[0].outcome, "recorded")
            assert app_rec.disposition_snapshot is not None
            self.assertEqual(app_rec.disposition_snapshot["schema_version"], "2.0")
            app_manifest = app_rec.disposition_snapshot["state_manifest"]
            assert isinstance(app_manifest, dict)
            self.assertEqual(app_manifest["item_count"], 10)
            self.assertTrue(app_rep.representable)
            self.assertGreater(app_rep.disposition_bytes, 1_000)

            # Cold fresh-process LocalSessionLedger (empty _part_documents cache, disk .evidence fallback):
            cold_dir = Path(paged_tmp) / "cold_v1"
            cold_ledger = LocalSessionLedger(
                cold_dir,
                enable_disposition_snapshot_writes=True,
                evidence_budget=EvidenceReadBudget(),
            )
            cold_ledger.initialize(IDENTITY, now=fixed_now)
            cold_ledger.replace(
                IDENTITY,
                lambda r: r.evolve(
                    completed_initial_reviews=1,
                    last_committed_reservation_id="2" * 64,
                    transaction=tx,
                ),
                now=fixed_now,
            )
            cold_rec_1, _ = apply_session_command(
                cold_ledger,
                IDENTITY,
                MaintainerCommand(
                    action="accept-coverage-uncertainty",
                    actor="alice",
                    reason="ok",
                    head_sha=head_sha,
                ),
                actor_role="OWNER",
                policy_digest="c" * 64,
                provenance="comment:52",
                incomplete_coverage=tuple(
                    f"file:{_deterministic_valid_path(i, total_len=227)}:budget-exhausted"
                    for i in range(42)
                ),
                now=fixed_now,
            )
            cold_disk_only = LocalSessionLedger(
                cold_dir,
                enable_disposition_snapshot_writes=True,
                evidence_budget=EvidenceReadBudget(),
            )
            self.assertEqual(cold_disk_only._part_documents, {})
            cold_budget = EvidenceReadBudget()
            _, _, cold_rep = preview_session_command_representability(
                identity=IDENTITY,
                current_record=cold_rec_1,
                command=MaintainerCommand(
                    action="verify", actor="alice", head_sha=head_sha
                ),
                intent_sha256=intent_sha,
                binding=binding,
                actor_role="OWNER",
                enable_disposition_snapshot_writes=True,
                evidence_budget=cold_budget,
                source_ledger=cold_disk_only,
                now=fixed_now,
            )
            self.assertEqual(cold_budget.calls, 0)
            self.assertTrue(cold_rep.representable)

            # Inline v1 snapshot logical disposition_bytes decoding + hosted comment-map preloading:
            inline_v1_ledger = LocalSessionLedger(
                Path(paged_tmp) / "v1_inline",
                enable_disposition_snapshot_writes=True,
                evidence_budget=EvidenceReadBudget(),
            )
            iv1 = inline_v1_ledger.initialize(IDENTITY, now=fixed_now)
            iv1 = inline_v1_ledger.replace(
                IDENTITY,
                lambda r: r.evolve(
                    completed_initial_reviews=1,
                    last_committed_reservation_id="2" * 64,
                    transaction=tx,
                ),
                now=fixed_now,
            )
            iv1, iv1_res = apply_session_command(
                inline_v1_ledger,
                IDENTITY,
                MaintainerCommand(
                    action="accept-risk",
                    actor="alice",
                    reason="inline-v1",
                    head_sha=head_sha,
                    finding_references=refs_10[:2],
                ),
                finding_inventory=inv_10,
                actor_role="OWNER",
                policy_digest="c" * 64,
                provenance="comment:60",
                now=fixed_now,
            )
            # Force inline v1 snapshot via inline_manifest:
            from review_sensei.bounded_evidence import inline_manifest

            iv1_doc = {
                "schema_version": "1.0",
                "result_digest": "f" * 64,
                "dispositions": [dict(d) for d in iv1.dispositions],
            }
            iv1_binding = {
                "repository": IDENTITY.repository,
                "repository_id": IDENTITY.repository_id,
                "pull_request": IDENTITY.pull_request,
                "base_sha": "a" * 40,
                "head_sha": head_sha,
                "policy_digest": "c" * 64,
                "configuration_digest": "d" * 64,
                "generation": iv1.generation,
                "producer": "local-ledger",
                "purpose": "dispositions",
                "schema_version": "1.0",
            }
            iv1_inline_manifest = inline_manifest(
                iv1_doc, binding=iv1_binding, item_count=len(iv1.dispositions)
            )
            assert iv1_inline_manifest is not None
            iv1_snap = iv1.evolve(
                dispositions=(),
                disposition_snapshot={
                    "schema_version": "1.0",
                    "result_digest": "f" * 64,
                    "state_manifest": iv1_inline_manifest,
                },
            )
            inline_v1_eval = evaluate_terminal_action_representability(
                identity=IDENTITY,
                intent_sha256=intent_sha,
                outcome={"kind": "unknown_write_outcome", "reason": "ambiguous"},
                result_document=asdict(iv1_res),
                binding=binding,
                current_record=iv1_snap,
            )
            self.assertGreater(inline_v1_eval.disposition_bytes, 200)
            self.assertTrue(inline_v1_eval.representable)

    def test_exact_8192_manifest_byte_boundary_8191_8192_8193(self) -> None:
        """Exact 8,192-byte inline manifest boundary (8191 ok, 8192 ok, 8193 refused)."""
        from review_sensei.bounded_evidence import encode_evidence

        buf = bytearray()
        block = 0
        while len(buf) < 7139:
            for val in hashlib.sha256(f"boundary:{block}".encode("ascii")).digest():
                buf.append(ord(_ALPHABET[val & 63]))
            block += 1
        summary_text = buf[:7139].decode("ascii")
        result_doc = {
            "action": "accept-risk",
            "applied": True,
            "summary": summary_text,
        }
        outcome = {"kind": "applied", "reason": None}

        for producer, expected_raw_bytes, expected_ok in (
            ("github-bot:99", 8_191, True),
            ("github-bot:999", 8_192, True),
            ("github-bot:9999", 8_193, False),
        ):
            binding = build_action_result_binding(
                identity=IDENTITY,
                policy_commit="a" * 40,
                head_sha="b" * 40,
                intent_sha256="1" * 64,
                configuration_digest="d" * 64,
                owner_generation=1,
                producer=producer,
            )
            envelope = {
                "schema_version": "caller-action-result-v1",
                "intent_sha256": "1" * 64,
                "outcome": outcome,
                "result": result_doc,
            }
            carrier = {
                "schema_version": "caller-action-result-v1",
                "payload": encode_evidence(envelope, max_decoded_bytes=2_097_152),
            }
            raw_manifest = {
                "encoding": "inline-json-v1",
                "binding": dict(binding),
                "item_count": 1,
                "sha256": hashlib.sha256(canonical_bytes(carrier)).hexdigest(),
                "document": carrier,
            }
            self.assertEqual(len(canonical_bytes(raw_manifest)), expected_raw_bytes)

            rep = evaluate_terminal_action_representability(
                identity=IDENTITY,
                intent_sha256="1" * 64,
                outcome=outcome,
                result_document=result_doc,
                binding=binding,
            )
            self.assertEqual(rep.representable, expected_ok)
            if expected_ok:
                self.assertEqual(rep.manifest_bytes, expected_raw_bytes)
                self.assertIsNone(rep.refusal_code)
            else:
                self.assertIsNone(rep.manifest_bytes)
                self.assertEqual(rep.refusal_code, "carrier_inline_bound_exceeded")

    def test_physical_budget_pricing_fence_tail_cold_snapshot_reads_and_intermediate_bounds(
        self,
    ) -> None:
        """Cover startup, ancestry, cold snapshot reads, retries, ambiguous writes, 4-call fence tail, and intermediate bounds."""
        from review_sensei_github.action_representability import (
            _command_pricing,
            _unlisted_snapshot_part_reads,
        )

        # 4-call protected terminal tail: 60 ordinary reads fit (64 - 4 = 60); 61 refuses:
        p_ok = ActionSettlementPricing(
            startup_requests=4,
            ancestry_read_requests=2,
            snapshot_read_requests=2,
            retry_reconcile_requests=1,
            ambiguous_write_charges=3,
            business_mutation_requests=3,
            refinalization_requests=3,
            receipt_publication_requests=2,
            terminal_close_requests=1,
            committed_requests=10,
            ordinary_evidence_reads=58,  # 58 + 2 snapshot reads == 60 == cap!
            protected_fence_reads=4,
            evidence_read_limit=64,
            original_api_limit=512,
        )
        self.assertTrue(p_ok.fits())
        self.assertEqual(p_ok.ordinary_evidence_read_cap, 60)

        p_tail_overflow = ActionSettlementPricing(
            ordinary_evidence_reads=60,
            snapshot_read_requests=1,  # 61 > 60 cap!
            protected_fence_reads=4,
            evidence_read_limit=64,
            original_api_limit=512,
        )
        self.assertFalse(p_tail_overflow.fits())
        rep_tail = evaluate_terminal_action_representability(
            identity=IDENTITY,
            intent_sha256="1" * 64,
            outcome={"kind": "applied", "reason": None},
            result_document={"applied": True, "summary": "ok"},
            binding=self._default_binding(),
            pricing=p_tail_overflow,
        )
        self.assertFalse(rep_tail.representable)
        self.assertEqual(
            rep_tail.refusal_code, "request_budget_exhausted_before_effect"
        )
        self.assertIn("preserving 4 fence reads", rep_tail.reason)

        with self.assertRaises(ReviewInputError):
            ActionSettlementPricing(protected_fence_reads=64, evidence_read_limit=64)

        # Cold unlisted vs warm listed snapshot part reads on hosted ledger:
        from review_sensei.bounded_evidence import evidence_manifest, partition_evidence

        snap_binding = {
            "repository": IDENTITY.repository,
            "repository_id": IDENTITY.repository_id,
            "pull_request": IDENTITY.pull_request,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "policy_digest": "c" * 64,
            "configuration_digest": "d" * 64,
            "generation": 0,
            "producer": "github-bot:999",
            "purpose": "dispositions",
            "schema_version": "1.0",
        }
        snap_doc = {
            "schema_version": "1.0",
            "result_digest": "f" * 64,
            "dispositions": [
                {
                    "id": i,
                    "blob": "".join(
                        hashlib.sha256(f"part:{i}:{k}".encode("ascii")).hexdigest()
                        for k in range(600)
                    ),
                }
                for i in range(2)
            ],
        }
        snap_parts = partition_evidence(snap_doc, binding=snap_binding, item_count=2)
        self.assertEqual(len(snap_parts), 2)
        part_ids = tuple(str(1001 + idx) for idx in range(len(snap_parts)))
        snap_record = SessionRecord.create(IDENTITY).evolve(
            disposition_snapshot={
                "schema_version": "1.0",
                "result_digest": "f" * 64,
                "state_manifest": evidence_manifest(
                    snap_doc,
                    binding=snap_binding,
                    item_count=2,
                    parts=snap_parts,
                    storage_ids=part_ids,
                ),
            }
        )
        hosted_ledger = SimpleNamespace(
            _comments_by_id={1001: {"id": 1001}},
            _verified_parts={},
        )
        self.assertEqual(
            _unlisted_snapshot_part_reads(
                hosted_ledger,
                snap_record,
                action="accept-risk",
                writes_enabled=True,
            ),
            1,
        )
        caller = SimpleNamespace(
            accounting=SimpleNamespace(
                configuration=SimpleNamespace(limit=64),
                blocks=(),
            ),
            usable=50,
            _block="b0",
        )
        priced = _command_pricing(
            caller,
            action="accept-risk",
            writes_enabled=True,
            ledger=hosted_ledger,
            current_record=snap_record,
        )
        self.assertEqual(priced.snapshot_read_requests, 1)

        # Warm comment-map + startup-snapshot extraction with 0 network/budget calls:
        from review_sensei.terminal_representability import (
            _InMemoryPreviewLedger,
            _preload_snapshot_parts_from_ledger,
        )

        part_body_1 = (
            "ReviewSensei immutable evidence part v1\n```json\n"
            + canonical_bytes(snap_parts[0]).decode("utf-8")
            + "\n```\n<!-- reviewsensei:evidence-part:v1 -->"
        )
        part_body_2 = (
            "ReviewSensei immutable evidence part v1\n```json\n"
            + canonical_bytes(snap_parts[1]).decode("utf-8")
            + "\n```\n<!-- reviewsensei:evidence-part:v1 -->"
        )
        warm_hosted = SimpleNamespace(
            evidence_producer=lambda: "github-bot:999",
            _verified_parts={},
            _comments_by_id={1001: {"id": 1001, "body": part_body_1}},
            _quarantine_startup_snapshot=SimpleNamespace(
                comments_by_id={1002: {"id": 1002, "body": part_body_2}}
            ),
        )
        warm_budget = EvidenceReadBudget()
        preloaded_warm = _preload_snapshot_parts_from_ledger(
            warm_hosted,
            IDENTITY,
            snap_record,
            producer="github-bot:999",
        )
        self.assertEqual(len(preloaded_warm), 2)
        mem_hosted = _InMemoryPreviewLedger(
            snap_record,
            producer="github-bot:999",
            preloaded_parts=preloaded_warm,
            evidence_budget=warm_budget,
        )
        self.assertEqual(len(mem_hosted._preloaded_parts), 2)
        self.assertEqual(warm_budget.calls, 0)
        chained_parts = _preload_snapshot_parts_from_ledger(
            mem_hosted,
            IDENTITY,
            snap_record,
            producer="github-bot:999",
        )
        self.assertEqual(len(chained_parts), 2)

        bad_framing_hosted = SimpleNamespace(
            evidence_producer=lambda: "github-bot:999",
            _comments_by_id={1001: {"id": 1001, "body": "bad"}},
        )
        with self.assertRaisesRegex(
            ReviewInputError, "GitHub partition framing is invalid"
        ):
            _preload_snapshot_parts_from_ledger(
                bad_framing_hosted,
                IDENTITY,
                snap_record,
                producer="github-bot:999",
            )

        # Intermediate SessionRecord overflow caught even when terminal state shrinks:
        from review_sensei.disposition import FindingDisposition

        small_terminal = SessionRecord.create(IDENTITY)
        bloated_inter = small_terminal.evolve(
            dispositions=tuple(
                FindingDisposition(
                    fingerprint=f"{i:064x}",
                    action="accept-risk",
                    reason="r" * 450,
                    actor="alice",
                    head_sha="b" * 40,
                    actor_role="OWNER",
                    finding_reference=f"RS-{i:06X}",
                    recorded_at="2026-11-11T00:00:00Z",
                    policy_digest="c" * 64,
                    provenance=f"comment:{i + 1}",
                    target_kind="finding",
                ).to_dict()
                for i in range(10)
            )
        )
        inter_base = estimate_session_record_bytes(bloated_inter)
        rep_inter = evaluate_terminal_action_representability(
            identity=IDENTITY,
            intent_sha256="1" * 64,
            outcome={"kind": "applied", "reason": None},
            result_document={"applied": True, "summary": "ok"},
            binding=self._default_binding(),
            current_record=small_terminal,
            intermediate_record=bloated_inter,
            future_reserve_bytes=(MAX_SESSION_RECORD_BYTES - inter_base + 64),
        )
        self.assertFalse(rep_inter.representable)
        self.assertEqual(rep_inter.refusal_code, "session_root_bytes_exceeded")
        self.assertLessEqual(rep_inter.projected_record_bytes, MAX_SESSION_RECORD_BYTES)
        self.assertGreater(
            rep_inter.max_intermediate_record_bytes, MAX_SESSION_RECORD_BYTES
        )

    def test_ordinary_hosted_command_to_retention_to_settlement_two_decision_snapshot_guard(
        self,
    ) -> None:
        """Full ordinary hosted command -> snapshot retention -> settlement guard on 2-decision snapshot."""
        from review_sensei.bounded_evidence import (
            evidence_manifest,
            partition_evidence,
        )
        from review_sensei.disposition import FindingDisposition

        with tempfile.TemporaryDirectory() as directory:
            f = _G34CommandFixture(directory, limit=512)
            patch_file = f.root / "review.patch"
            base_patch = patch_file.read_text(encoding="utf-8")
            # 37 binary files -> 37 * 249B file tokens + 1 mandatory 71B result token = 38 tokens!
            binary_entries = "".join(
                f"\ndiff --git a/{p} b/{p}\nnew file mode 100644\n"
                f"index 0000000..1111111\nBinary files /dev/null and b/{p} differ\n"
                for p in (
                    f"services_{i:02d}/domain_{i:02d}/"
                    + _deterministic_valid_path(i, total_len=203)
                    for i in range(37)
                )
            )
            patch_file.write_text(
                base_patch + binary_entries, encoding="utf-8", newline="\n"
            )
            self.assertEqual(f.invoke(human=True), 0, f.error)

            base_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert base_rec is not None and base_rec.transaction is not None
            index = parse_finding_index(
                f.reviews[-1]["body"], repository_id=123, pull_request=7, head_sha=HEAD
            )
            assert index is not None
            finding_fp = index.entries[0].instance
            finding_ref = "RS-" + finding_fp[:6].upper()

            review_json = json.loads(
                (f.root / "review.json").read_text(encoding="utf-8")
            )
            loaded_result = ReviewResult.from_dict(review_json)
            inc_tokens = incomplete_coverage_from_result(loaded_result)
            self.assertEqual(len(inc_tokens), 38)

            # Seed the hosted PR ledger with the authenticated 2-decision disposition_snapshot:
            disp_1 = FindingDisposition(
                fingerprint=finding_fp,
                action="accept-risk",
                reason="acceptable risk",
                actor="maintainer",
                head_sha=HEAD,
                actor_role="OWNER",
                finding_reference=finding_ref,
                recorded_at="2026-11-11T00:00:00Z",
                policy_digest=base_rec.transaction.policy_digest,
                provenance="comment:201",
                target_kind="finding",
            ).to_dict()
            disp_2 = FindingDisposition(
                fingerprint=hashlib.sha256(
                    (
                        f"coverage-uncertainty:{HEAD}:{base_rec.transaction.policy_digest}:"
                        f"{','.join(inc_tokens)}"
                    ).encode("utf-8")
                ).hexdigest(),
                action="accept-coverage-uncertainty",
                reason="ok",
                actor="maintainer",
                head_sha=HEAD,
                actor_role="OWNER",
                recorded_at="2026-11-11T00:00:00Z",
                policy_digest=base_rec.transaction.policy_digest,
                provenance="comment:202",
                target_kind="coverage_uncertainty",
                incomplete_coverage=inc_tokens,
            ).to_dict()
            from review_sensei.session import _stored_dispositions

            stored_two = _stored_dispositions([disp_1, disp_2], snapshot=True)
            bot_id = f.comment["user"]["id"]
            snap_binding = {
                "repository": IDENTITY.repository,
                "repository_id": IDENTITY.repository_id,
                "pull_request": IDENTITY.pull_request,
                "base_sha": base_rec.transaction.base_sha,
                "head_sha": HEAD,
                "policy_digest": base_rec.transaction.policy_digest,
                "configuration_digest": base_rec.transaction.configuration_digest,
                "generation": base_rec.generation,
                "producer": f"github-bot:{bot_id}",
                "purpose": "dispositions",
                "schema_version": "1.0",
            }
            snap_doc = {
                "schema_version": "1.0",
                "result_digest": base_rec.transaction.result_sha256,
                "dispositions": list(stored_two),
            }
            parts = partition_evidence(snap_doc, binding=snap_binding, item_count=2)
            self.assertEqual(len(parts), 1)
            part_comment_id = 1_500_001
            part_raw = canonical_bytes(parts[0]).decode("utf-8")
            f.extra_comments[part_comment_id] = {
                **f.comment,
                "id": part_comment_id,
                "body": (
                    f"ReviewSensei immutable evidence part v1\n```json\n"
                    f"{part_raw}\n```\n<!-- reviewsensei:evidence-part:v1 -->"
                ),
            }
            snap_manifest = evidence_manifest(
                snap_doc,
                binding=snap_binding,
                item_count=2,
                parts=parts,
                storage_ids=(str(part_comment_id),),
            )
            seeded_rec = base_rec.evolve(
                dispositions=(),
                disposition_snapshot={
                    "schema_version": "1.0",
                    "result_digest": base_rec.transaction.result_sha256,
                    "state_manifest": snap_manifest,
                },
            )
            f.comment["body"] = render_session_comment(
                record=seeded_rec, repository_id=123, pull_request=7
            )

            # Replay '@reviewsensei override RS-...' through the ordinary hosted command route!
            # Without the G34 fix, preview stripped disposition_snapshot and allowed execution,
            # causing terminal action settlement to crash > 8,192B on stored[-1]!
            f.SOURCE_ID = 405
            f.select_command(
                f"@reviewsensei override {finding_ref} acceptable risk",
                new_run=True,
            )
            self.assertEqual(f.invoke_command(), 0, f.error + f.stdout)
            self.assertEqual(f.business_writes, 0)
            self.assertIn("carrier_inline_bound_exceeded", f.stdout)

            settled_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert settled_rec is not None
            self.assertEqual(
                settled_rec.disposition_snapshot, seeded_rec.disposition_snapshot
            )
            settled_state = ApiRunAccounting.from_document(
                settled_rec.api_run_accounting
            )
            assert settled_state.action is not None
            self.assertEqual(settled_state.action["phase"], "terminal")
            self.assertEqual(settled_state.action["outcome"]["kind"], "explicit_noop")


if __name__ == "__main__":
    unittest.main()
