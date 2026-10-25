"""Source-selected conservative caller liabilities for one hosted lifecycle.

This is a qualification plan, not a repository default or Worker cost model.
Unused/abandoned reservations stay occupied. Every dispatch is charged once by
ApiRunRequestTransport; these bounds protect other work, not a second counter.
"""

from __future__ import annotations

from dataclasses import dataclass

from .http import MAX_PAGINATION_ITEMS, MAX_PAGINATION_PAGES
from .operation_comments import MAX_RESULT_PARTS
from .session_ledger import _COMMENT_PAGE_SIZES

# Budgeted native _fetch_pages is not GitHubHttp.paginate's ten-request walk.
# It can reset its size ladder after every positive page. Each successful full
# page advances >=1 item, plus a terminal page, with at most one complete ladder
# of oversized attempts at each offset. Preserve that complete finite bound.
NATIVE_LIST_CALLS = (MAX_PAGINATION_ITEMS + 1) * len(_COMMENT_PAGE_SIZES)
# Evidence-bound replace: discovery, rediscovery, preactivation, readback, PATCH.
NATIVE_BOUND_REPLACE_CALLS = 4 * NATIVE_LIST_CALLS + 1


@dataclass(frozen=True)
class CallerLiability:
    purpose: str
    invocations: int
    requests_each: int
    source: str

    @property
    def requests(self) -> int:
        return self.invocations * self.requests_each


# Complete new-review, at most one file/finding, inline baseline, no preexisting
# decided threads. Unknown/oversized output refuses; it cannot erase findings.
# The two storage carriers retain their existing32-part ceilings. No partitioned
# publication/journal prototype is enabled by this ordinary-publication plan.
ADMISSION = (
    CallerLiability(
        "session-observation", 3, NATIVE_LIST_CALLS, "cli._main / prepare_session_round"
    ),
    CallerLiability(
        "reserve-and-attach",
        2,
        NATIVE_BOUND_REPLACE_CALLS,
        "prepare_review_transaction",
    ),
)
PROVIDER_METADATA = (
    CallerLiability(
        "provider-history",
        2,
        MAX_PAGINATION_PAGES,
        "run_actions_analysis / ResultPartStore.read",
    ),
    CallerLiability("provider-bot", 1, 1, "run_actions_analysis"),
    CallerLiability(
        "provider-event-writes",
        3,
        2,
        "run_admitted_trigger / _complete_retained_computation",
    ),
    CallerLiability(
        "provider-result-parts",
        MAX_RESULT_PARTS,
        2,
        "ResultPartStore.write / operation port readback",
    ),
)
COMPLETION = (
    CallerLiability("retained-bot-pr", 2, 1, "publication_recovery._store"),
    CallerLiability(
        "retained-complete-lists",
        3,
        MAX_PAGINATION_PAGES,
        "retain_checkpoint_review_result / unknown write reconciliation",
    ),
    CallerLiability(
        "retained-result-parts", MAX_RESULT_PARTS, 2, "ResultPartStore.write"
    ),
    CallerLiability(
        "analysis-checkpoint",
        1,
        NATIVE_BOUND_REPLACE_CALLS,
        "checkpoint_review_analysis",
    ),
)
# Separate fresh source fences, never a cached response/authority. The ordinary
# reader/store/app/finalizer use these12 stages for this bounded workload.
_PUBLICATION_FENCES = (
    "load-result",
    "retention",
    "admission-context",
    "prepare",
    "inventory-start",
    "inventory-readback",
    "inventory-read",
    "before-root",
    "root-readback",
    "finalizer-source",
    "finalizer-approval",
    "cleanup-source",
)
_PUBLICATION_HISTORIES = (
    "prepared-reviews",
    "prepared-files",
    "original-inline",
    "inventory-discovery",
    "intent-readback",
    "parts-readback",
    "retained-result-readback",
    "before-post-reviews",
    "after-post-reviews",
    "finalizer-reviews",
    "finalizer-inline",
    "finalizer-threads",
    "terminal-visible-root",
)
PUBLICATION = (
    CallerLiability(
        "publication-authority",
        len(_PUBLICATION_FENCES),
        NATIVE_LIST_CALLS + 2,
        "application / default inventory / approval fresh ledger+Bot+PR",
    ),
    CallerLiability(
        "publication-history",
        len(_PUBLICATION_HISTORIES),
        64,
        "publisher/finalizer complete review/file/inline/thread walks (max_requests=64)",
    ),
    CallerLiability(
        "publication-carriers",
        2 * MAX_RESULT_PARTS,
        2,
        "retained result and default human inventory parts",
    ),
    CallerLiability("inventory-intent", 1, 2, "HumanInventoryStore.stage"),
    CallerLiability(
        "publication-write-intent",
        1,
        NATIVE_BOUND_REPLACE_CALLS,
        "retain_review_result pre-POST uncertainty fence",
    ),
    CallerLiability(
        "publication-progress",
        1,
        NATIVE_BOUND_REPLACE_CALLS,
        "record_admitted_blocker_progress",
    ),
    CallerLiability(
        "capability-and-gate-effects",
        2,
        1 + 2 + 5,
        "brokerPOST + check find/write + normal approval fences",
    ),
    CallerLiability(
        "native-root-and-approval",
        2,
        2,
        "ordinary COMMENT and normal APPROVE with owned readback",
    ),
    CallerLiability(
        "workflow-head-guard",
        3,
        1,
        "review-sensei-run.yml pre-publication head revalidation",
    ),
)
# Analysis failure is the largest cleanup branch: the fresh ownership read,
# failed-attempt's own fresh read, then abort/reserve/commit. The eight-call gate
# tail is also protected. Exactly one cleanup owner claims this reservation.
TERMINAL = (
    CallerLiability(
        "cleanup-ownership-reads",
        2,
        NATIVE_LIST_CALLS,
        "cli cleanup_analysis_reservation / record_session_failed_attempt",
    ),
    CallerLiability(
        "terminal-native-mutations",
        3,
        NATIVE_BOUND_REPLACE_CALLS,
        "record_session_failed_attempt abort/reserve/commit; normal publication uses one",
    ),
    CallerLiability(
        "terminal-gate-tail",
        1,
        1 + 2 + 5,
        "brokerPOST + check find/write + approval fences",
    ),
)
WORK_REQUESTS = sum(
    row.requests for row in (*ADMISSION, *PROVIDER_METADATA, *COMPLETION, *PUBLICATION)
)
TERMINAL_REQUESTS = sum(row.requests for row in TERMINAL)
OWNER_REQUESTS = WORK_REQUESTS + TERMINAL_REQUESTS


def qualification_limit(startup_requests: int) -> int:
    """One initial owner and one complete successor; no limit selection policy."""
    return startup_requests + 2 * OWNER_REQUESTS
