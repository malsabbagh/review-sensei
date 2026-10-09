"""Offline golden checks against the immutable published v0.6.16 source.

Requires that commit in the local object database. No fetch, checkout, writes to
another worktree, provider calls or GitHub requests occur. Archives are temporary.
The legacy oversized-comment gap is recorded, not asserted to be safe.
"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "5a121dfe39fb978c7d71def0d08dc570aad83f1d"


def marker(document):
    encoded = base64.urlsafe_b64encode(json.dumps(document).encode()).decode()
    return f"<!-- reviewsensei:eligibility:v1 {encoded} -->"


def fixtures():
    sys.path.insert(0, str(ROOT / "src"))
    from review_sensei.baseline import (
        BaselineFinding,
        ReviewBaseline,
        baseline_history_document,
    )
    from review_sensei.bounded_evidence import decode_evidence, encode_evidence
    from review_sensei.context import ReviewContextCacheKey
    from review_sensei.hosting.github.approval import (
        ApprovalFacts,
        ReviewApprovalEligibility,
    )
    from review_sensei.hosting.github.session_ledger import render_session_comment
    from review_sensei.human_assessment import HumanReviewFinding, PendingHumanReview
    from review_sensei.models import build_transaction_configuration_context
    from review_sensei.session import SessionIdentity, SessionRecord

    finding = HumanReviewFinding("f" * 64, "src/a.py", "Confirm bounded behavior.")
    pending = PendingHumanReview("a" * 40, (finding,))
    facts = ApprovalFacts(True, False, "complete", "legacy", False, True, None)
    eligibility = ReviewApprovalEligibility("b" * 40, "c" * 64, facts, pending)
    v2 = replace(
        eligibility,
        human_review=replace(
            pending,
            findings=(replace(finding, required_paths=("src/a.py", "src/b.py")),),
        ),
    )
    clean = replace(
        eligibility,
        human_review=None,
        facts=replace(facts, has_human_adjudication_findings=False),
    )
    unknown = deepcopy(clean.to_dict())
    unknown["schema_version"] = "future"
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)
    record = SessionRecord.create(SessionIdentity("owner/repo", 1, 99), now=now)
    body = render_session_comment(repository_id=99, pull_request=1, record=record)
    configuration = build_transaction_configuration_context(
        provider={
            "name": "fixture",
            "profile": None,
            "base_url": None,
            "timeout_seconds": None,
            "max_output_tokens": None,
            "allow_custom_endpoint": False,
            "openrouter_policy": None,
        },
        model="fixture",
        stages=[],
        category_policy=[],
        publication_mode="legacy",
        orchestration_enabled=False,
    )
    unified = deepcopy(configuration)
    unified["orchestration"]["work_policy_digest"] = "d" * 64
    richer_human = replace(
        eligibility,
        human_review=replace(
            pending,
            findings=tuple(
                replace(
                    finding, fingerprint=f"{i:064x}", body=f"Concern {i}: " + "x" * 2100
                )
                for i in range(21)
            ),
        ),
    )
    baseline = ReviewBaseline(
        cache_key=ReviewContextCacheKey(
            repository="owner/repo",
            pull_request=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
            engine="fixture",
            model="fixture",
            profile="default",
            stage_digest="c" * 64,
            context_digest="d" * 64,
            learning_digest="e" * 64,
        ),
        policy_digest="f" * 64,
        complete=True,
        findings=tuple(
            BaselineFinding(f"{i:064x}", f"{i + 20:064x}", path="src/a.py")
            for i in range(12)
        ),
        reviewed_paths=("src/a.py",),
    )
    richer_session = SessionRecord.create(
        SessionIdentity("owner/repo", 1, 99),
        now=now,
        convergence_history={
            "state": "completed",
            "baseline": baseline_history_document(baseline, require_complete=True),
            "progress": [],
            "provenance": {"ledger_digest": "a" * 64},
        },
    )
    # Structural reader limits, deliberately compressible. These fixtures
    # qualify closed-schema boundaries, not realistic capacity distributions.
    boundary_sessions = []
    for count in (4, 50, 512):
        boundary = replace(
            baseline,
            findings=tuple(
                BaselineFinding(f"{i:064x}", f"{i + 1024:064x}", path="src/a.py")
                for i in range(count)
            ),
        )
        boundary_sessions.append(
            SessionRecord.create(
                SessionIdentity("owner/repo", 1, 99),
                now=now,
                convergence_history={
                    "state": "completed",
                    "baseline": baseline_history_document(
                        boundary, require_complete=True
                    ),
                    "progress": [],
                    "provenance": {"ledger_digest": "a" * 64},
                },
            ).to_dict()
        )
    expanded_boundary = decode_evidence(
        boundary_sessions[-1]["convergence_history"]["baseline"],
        max_encoded_bytes=11264,
        max_decoded_bytes=2097152,
    )
    expanded_boundary["findings"].append(
        {
            **expanded_boundary["findings"][-1],
            "fingerprint": f"{512:064x}",
            "resolution_criterion": f"{1536:064x}",
        }
    )
    return {
        "v3": richer_human.to_dict(),
        "encoded_baseline_session": richer_session.to_dict(),
        "encoded_boundary_sessions": boundary_sessions,
        "over_boundary_baseline": encode_evidence(
            expanded_boundary, max_decoded_bytes=2097152
        ),
        "v1": eligibility.to_dict(),
        "v2": v2.to_dict(),
        "clean": marker(clean.to_dict()),
        "unsupported": marker(unknown),
        "session": record.to_dict(),
        "session_body": body,
        "expanded_session": "x" * (17 * 1024) + "\n" + body,
        "oversized_session": "x" * (41 * 1024) + "\n" + body,
        "configuration": configuration,
        "unified_configuration": unified,
    }


def worker(source: Path, fixture: Path, legacy: bool):
    source = source.resolve()
    sys.path.insert(0, str(source))
    import review_sensei
    from review_sensei.baseline import baseline_from_history_document
    from review_sensei.errors import ReviewInputError
    from review_sensei.hosting.github.approval import ReviewApprovalEligibility
    from review_sensei.hosting.github.errors import GitHubPublicationError
    from review_sensei.hosting.github.http import GitHubHttp
    from review_sensei.hosting.github.publication import ReviewApprovalFinalizer
    from review_sensei.hosting.github.session_ledger import (
        GitHubIssueCommentSessionLedger,
    )
    from review_sensei.models import ReviewTransaction
    from review_sensei.planning import MAX_RELATED_PATHS
    from review_sensei.session import (
        MAX_CONVERGENCE_HISTORY_BYTES,
        MAX_SESSION_COMMENT_BYTES,
        MAX_SESSION_RECORD_BYTES,
        SessionIdentity,
        SessionRecord,
    )

    if not Path(review_sensei.__file__).resolve().is_relative_to(source):
        raise AssertionError("reader was not imported from the selected immutable tree")
    data = json.loads(fixture.read_text())
    assert (
        MAX_RELATED_PATHS,
        MAX_CONVERGENCE_HISTORY_BYTES,
        MAX_SESSION_RECORD_BYTES,
        MAX_SESSION_COMMENT_BYTES,
    ) == ((32, 4096, 8192, 16384) if legacy else (64, 12288, 20480, 40960))
    assert ReviewApprovalEligibility.from_dict(data["v1"]).to_dict() == data["v1"]
    if legacy:
        try:
            ReviewApprovalEligibility.from_dict(data["v2"])
        except ReviewInputError:
            pass
        else:
            raise AssertionError("legacy reader accepted richer requirements")
    else:
        assert ReviewApprovalEligibility.from_dict(data["v2"]).to_dict() == data["v2"]
    assert SessionRecord.from_dict(data["session"]).to_dict() == data["session"]
    for parser, document in [
        (ReviewApprovalEligibility.from_dict, data["v3"]),
        (SessionRecord.from_dict, data["encoded_baseline_session"]),
        *[
            (SessionRecord.from_dict, item)
            for item in data["encoded_boundary_sessions"]
        ],
    ]:
        try:
            restored = parser(document)
        except ReviewInputError:
            assert legacy
        else:
            assert not legacy
            assert restored.to_dict() == document
    if not legacy:
        for count, document in zip((4, 50, 512), data["encoded_boundary_sessions"]):
            restored = SessionRecord.from_dict(document)
            baseline = baseline_from_history_document(
                restored.convergence_history["baseline"]
            )
            assert len(baseline.findings) == count
    try:
        baseline_from_history_document(data["over_boundary_baseline"])
    except ReviewInputError:
        pass
    else:
        raise AssertionError("reader accepted findings above the structural ceiling")
    ReviewTransaction.compute_configuration_digest(data["configuration"])
    try:
        ReviewTransaction.compute_configuration_digest(data["unified_configuration"])
    except ReviewInputError:
        assert legacy
    else:
        assert not legacy

    # The synthetic transport can only answer reads. No live network or writes.
    class Response:
        status = 200
        headers = {}

        def __init__(self, value):
            self.value = json.dumps(value).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return self.value[:size]

    for body in (
        marker(data["v2"]),
        marker(data["v3"]),
        data["unsupported"],
        "<!-- reviewsensei:eligibility:v1 broken -->",
    ):
        reviews = [
            {"commit_id": "b" * 40, "body": item, "user": {"login": "sensei[bot]"}}
            for item in (data["clean"], body)
        ]
        http = GitHubHttp(opener=lambda request, timeout: Response(reviews))
        finalizer = ReviewApprovalFinalizer(http=http)
        args = dict(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            head_sha="b" * 40,
            app_slug="sensei[bot]",
        )
        if not legacy and body in (marker(data["v2"]), marker(data["v3"])):
            expected = data["v2"] if body == marker(data["v2"]) else data["v3"]
            assert finalizer.load_eligibility(**args).to_dict() == expected
            continue
        assert finalizer.load_eligibility(**args) is None
        try:
            finalizer.load_eligibility(**args, require_valid=True)
        except GitHubPublicationError:
            pass
        else:
            raise AssertionError("unsupported latest record exposed older authority")

    items = [
        {
            "id": 1,
            "body": data["oversized_session"],
            "user": {"login": "sensei[bot]", "type": "Bot"},
        }
    ]

    def reads_only(request, timeout):
        assert request.method == "GET"
        return Response(items)

    ledger = GitHubIssueCommentSessionLedger(
        GitHubHttp(opener=reads_only), token="synthetic", app_slug="sensei[bot]"
    )
    identity = SessionIdentity("owner/repo", 1, 99)
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)
    for key in ("session_body", "expanded_session"):
        items[0]["body"] = data[key]
        expected = "missing" if legacy and key == "expanded_session" else "ok"
        assert ledger.load(identity, now=now).status == expected
    items[0]["body"] = data["oversized_session"]
    status = ledger.load(identity, now=now).status
    assert status == ("missing" if legacy else "integrity-failed")
    if not legacy:
        try:
            ledger.initialize(identity, now=now)
        except ReviewInputError:
            pass
        else:
            raise AssertionError("upgraded reader reset unreadable authority")
    print(
        f"PASS {'v0.6.16' if legacy else 'current'}: legacy and encoded evidence, latest authority, transaction identity, ledger reads"
    )
    if legacy:
        print(
            "LEGACY LIMIT: oversized trusted session comments are reported missing; retain upgraded readers and quiesce old writers before expanded writes"
        )


def main():
    if not __debug__:
        raise RuntimeError("reader checks require assertions; run without -O")
    if len(sys.argv) == 5 and sys.argv[1] == "--worker":
        worker(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4] == "legacy")
        return 0
    with tempfile.TemporaryDirectory(prefix="reviewsensei-readers-") as temporary:
        directory = Path(temporary)
        try:
            archive = subprocess.run(
                ["git", "archive", "--format=tar", BASELINE, "src"],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
        except (subprocess.CalledProcessError, OSError):
            print(
                "reader compatibility check: pinned v0.6.16 source is unavailable locally",
                file=sys.stderr,
            )
            return 1
        # Copy only regular archived files beneath src, never links or devices.
        with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
            for member in tree.getmembers():
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or path.parts[0] != "src":
                    raise ValueError("invalid archived reader path")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError("reader archive must contain regular files")
                destination = directory / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                stream = tree.extractfile(member)
                assert stream is not None
                destination.write_bytes(stream.read())
        fixture = directory / "synthetic-fixtures.json"
        fixture.write_text(json.dumps(fixtures()), encoding="utf-8")
        for source, mode in ((directory / "src", "legacy"), (ROOT / "src", "current")):
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(source),
                    str(fixture),
                    mode,
                ],
                cwd=directory,
                check=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
