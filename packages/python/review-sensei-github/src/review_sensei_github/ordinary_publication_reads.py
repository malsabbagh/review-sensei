"""Negative, invocation-local read bounds for qualified fresh ordinary routes.

The observation never supplies response bytes or native authority. Every read
still reaches the existing charged transport and every source validator runs.
"""

from __future__ import annotations

import re
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult

from .errors import GitHubHTTPError
from .http import GitHubHttp

# Complete conservative branches, not the normal-success trace alone: three
# check lookups plus lost POST readback; two PATCH readbacks; review lost-ack
# journal+marker reads; approval persisted+lost-ack journal+already reads;
# two approval PR fences and one complete thread scan. Checkpoint I/O is separate.
_ROLES = (
    ("checks", 4),
    ("check", 2),
    ("reviews", 5),
    ("pr", 2),
    ("threads", 1),
    # The journal opens even an empty exact inline inventory after a stored
    # root or approval response loss. These remain independent native reads.
    ("review_comments", 2),
)
_SOURCE: tuple[Any, ...] | None = None
_TRANSPORT_METHODS: dict[Any, Any] = {}
_ACTIVE: WeakKeyDictionary = WeakKeyDictionary()
_WITNESSES: WeakKeyDictionary = WeakKeyDictionary()
_WITNESS_SHAPES: WeakKeyDictionary = WeakKeyDictionary()
_OBSERVED: WeakKeyDictionary = WeakKeyDictionary()
_REMAINING: WeakKeyDictionary = WeakKeyDictionary()
_QUALIFIED: WeakKeyDictionary = WeakKeyDictionary()
_ISSUED_WITNESS: WeakKeyDictionary = WeakKeyDictionary()


def _install_publication_source(source_class):
    global _SOURCE
    from .checks import ReviewCheckPublisher
    from .publication import ReviewApprovalFinalizer, ReviewPublisher

    if _SOURCE is not None or source_class is not ReviewPublisher:
        raise ReviewInputError(
            "ordinary publication read source already installed or foreign"
        )
    from .api_run_requests import ApiRunRequestTransport
    from .finding_lifecycle import FindingLifecycleJournal
    from .ordinary_native_recovery import install_sources

    install_sources(
        ApiRunRequestTransport, FindingLifecycleJournal, ReviewCheckPublisher
    )
    classes = (
        ReviewPublisher,
        ReviewApprovalFinalizer,
        ReviewCheckPublisher,
        FindingLifecycleJournal,
    )
    methods = tuple(
        {
            name: getattr(cls, name)
            for name in dir(cls)
            if not name.startswith("__") and callable(getattr(cls, name))
        }
        for cls in classes
    )
    from .finding_lifecycle import _JournalTransport
    from .publication_recovery import PublicationBudgetHttp, _PaginationObservationHttp

    for cls in (
        GitHubHttp,
        _JournalTransport,
        PublicationBudgetHttp,
        _PaginationObservationHttp,
    ):
        _TRANSPORT_METHODS[cls] = cls.request
    _SOURCE = (classes, methods)


def _source_guard(publisher, journal):
    from .finding_lifecycle import FindingLifecycleJournal

    if _SOURCE is None or type(journal) is not FindingLifecycleJournal:
        raise ReviewInputError("ordinary publication read source unavailable")
    classes, methods = _SOURCE
    owners = (publisher, publisher.finalizer, publisher.checks, journal)
    if journal.reviewer is not publisher:
        raise ReviewInputError("ordinary publication journal owner changed")
    for owner, cls, expected in zip(owners, classes, methods, strict=True):
        if type(owner) is not cls or any(
            name in owner.__dict__ or getattr(cls, name) is not method
            for name, method in expected.items()
        ):
            raise ReviewInputError(
                "ordinary publication producer implementation changed"
            )
    # The application configures a publisher cleanup provider even for empty
    # reviews. Exact empty inputs below make _schedule_thread_cleanup return
    # before invoking it; any pending closure remains forbidden.
    if any(
        getattr(owner, name, None) is not None
        for owner, name in (
            (publisher.finalizer, "_operation_observer"),
            (publisher.finalizer, "_thread_cleanup_token_provider"),
            (publisher, "_pending_thread_cleanup"),
        )
    ):
        raise ReviewInputError("ordinary publication callback route is unqualified")
    return owners[:3]


def _require_reader_profile(publisher):
    from .application import GitHubApplication

    factories = {
        "human_inventory_reader_factory": GitHubApplication._human_inventory_reader,
        "quarantine_reader_factory": GitHubApplication._quarantine_hold_reader,
    }
    for name, source in factories.items():
        factory = getattr(publisher.finalizer, name)
        if factory is not None and (
            type(getattr(factory, "__self__", None)) is not GitHubApplication
            or getattr(factory, "__func__", None) is not source
        ):
            raise ReviewInputError("ordinary publication reader factory is unqualified")
    if (
        publisher.finalizer.historical_assessment_reader_factory is not None
        or publisher.finalizer.native_union_reader_factory is not None
        or publisher.finalizer.prior_publication_reader_factory is not None
    ):
        raise ReviewInputError("ordinary publication extended reader is unqualified")


def select_publication_observation(publisher, journal, *, result):
    """Choose an optional profile before issuing any observer or read quote.

    Unsupported initial implementations retain the conservative route. Once an
    observer exists, its original checks remain strict: this is not recovery
    from changed custody, failed qualification or an unfunded plan.
    """
    if any(
        active.publisher is publisher or active.journal is journal for active in _ACTIVE
    ):
        raise ReviewInputError("ordinary publication profile is already active")
    if _SOURCE is None or journal.reviewer is not publisher:
        raise ReviewInputError("ordinary publication journal source is unavailable")
    if result.provider == "openrouter":
        return nullcontext(None)
    try:
        owners = _source_guard(publisher, journal)
        _require_reader_profile(publisher)
        budget = (
            journal._native_budget
            or publisher.finalizer.evidence_budget
            or publisher.finalizer._operation_budget
        )
        if budget is None:
            return nullcontext(None)
        for owner in (*owners, journal):
            transport = owner._readback_http if owner is journal else owner.http
            _transport_signature(transport, journal, budget)
    except ReviewInputError:
        return nullcontext(None)
    return observe_publication(publisher, journal)


def unbound_population(journal, result, state):
    """Select only exact original criteria with no prior native finding target.

    This is a negative route selector, not authority. Qualification still binds
    the original transaction and source, and every replay reads native state.
    """
    from .lifecycle_admission import (
        initial_check_effects,
        restore_original_lifecycle_admission,
    )

    try:
        restore_original_lifecycle_admission(
            result, state, head_sha=journal.head_sha, base_sha=journal.base_sha
        )
    except (ReviewInputError, AttributeError):
        return False
    if journal.reviewer.native_finding_receipts(
        repository_id=journal.identity.repository_id,
        pull_request=journal.identity.pull_request,
    ):
        return False
    return initial_check_effects(journal.effects, journal.identity.repository)


def _transport_signature(http, journal, budget):
    from .finding_lifecycle import _JournalTransport
    from .publication_recovery import PublicationBudgetHttp, _PaginationObservationHttp

    result = []
    for _ in range(8):
        cls = type(http)
        if cls is _JournalTransport:
            if http.journal is not journal:
                raise ReviewInputError("ordinary publication transport journal changed")
            target = http.delegate
        elif cls is PublicationBudgetHttp:
            if http.publication_budget is not budget:
                raise ReviewInputError("ordinary publication transport budget changed")
            target = http.transport
        elif cls is _PaginationObservationHttp:
            if http.observation_budget is not budget:
                raise ReviewInputError(
                    "ordinary publication observation budget changed"
                )
            target = http.transport
        elif cls is GitHubHttp:
            if (
                http._quarantine_capacity_guard is not None
                or http._controlled_restart_guard is not None
            ):
                raise ReviewInputError(
                    "ordinary publication guarded route is unqualified"
                )
            target = None
        else:
            raise ReviewInputError(
                "ordinary publication custom transport is unqualified"
            )
        if (
            cls.request is not _TRANSPORT_METHODS.get(cls)
            or getattr(http.request, "__func__", None) is not cls.request
        ):
            raise ReviewInputError(
                "ordinary publication transport implementation changed"
            )
        result.append(
            (
                id(http),
                cls,
                cls.request,
                id(target),
                http.api_url,
                http.timeout,
                id(http.opener),
            )
        )
        if target is None:
            return tuple(result)
        http = target
    raise ReviewInputError("ordinary publication transport chain is unqualified")


@dataclass(frozen=True, eq=False)
class OrdinaryPublicationReadWitness:
    read_roles: tuple[tuple[str, int], ...]
    read_calls: int

    def require(self, journal, publisher):
        observation = _WITNESSES.get(self)
        if observation is None or _WITNESS_SHAPES.get(self) != (
            self.read_roles,
            self.read_calls,
        ):
            raise ReviewInputError(
                "ordinary publication read witness is unissued or changed"
            )
        _Observation._require(observation, journal, publisher)
        return self.read_calls


def issued_witness(journal):
    observation = getattr(journal.reviewer, "_ordinary_publication_observation", None)
    witness = _ISSUED_WITNESS.get(observation) if observation is not None else None
    if witness is not None:
        witness.require(journal, journal.reviewer)
    return witness


class _Observation:
    def __init__(self, publisher, journal, budget):
        self.publisher, self.journal, self.budget = publisher, journal, budget
        self.scope = (
            journal.ledger,
            journal.identity,
            journal.operation_id,
            journal.head_sha,
            journal.base_sha,
        )
        _OBSERVED[self] = {}
        self.bindings: list[tuple[Any, str, Any, Any, Any]] = []
        self.record: Any = None
        self.record_bytes = None
        self.result: Any = None
        self.result_bytes = None
        self.projected: Any = None
        self.projected_bytes = None
        self.witness = None

    def _require(self, journal, publisher):
        if (
            _ACTIVE.get(self) != (self.scope, tuple(self.bindings))
            or journal is not self.journal
            or publisher is not self.publisher
            or self.scope
            != (
                journal.ledger,
                journal.identity,
                journal.operation_id,
                journal.head_sha,
                journal.base_sha,
            )
        ):
            raise ReviewInputError("ordinary publication read scope expired or changed")
        if type(self) is not _Observation or any(
            name in self.__dict__ or getattr(_Observation, name) is not method
            for name, method in _OBSERVATION_METHODS.items()
        ):
            raise ReviewInputError(
                "ordinary publication observer implementation changed"
            )
        issued_witness = _ISSUED_WITNESS.get(self)
        if self.witness is not issued_witness:
            raise ReviewInputError("ordinary publication issued witness changed")
        _source_guard(publisher, journal)
        _require_reader_profile(publisher)
        for owner, attr, old, wrapper, signature in self.bindings:
            if (
                getattr(owner, attr) is not wrapper
                or wrapper.api_url != old.api_url
                or wrapper.timeout != old.timeout
                or type(wrapper) is not _ClosedPublicationHttp
                or wrapper.transport is not old
                or wrapper.observation is not self
                or type(wrapper).request is not _WRAPPER_REQUEST
                or getattr(wrapper.request, "__func__", None) is not _WRAPPER_REQUEST
                or _transport_signature(old, journal, self.budget) != signature
            ):
                raise ReviewInputError("ordinary publication transport custody changed")
        qualified = _QUALIFIED.get(self)
        if qualified is not None and (
            qualified
            != (
                self.record,
                self.record_bytes,
                self.result,
                self.result_bytes,
                self.projected,
                self.projected_bytes,
            )
            or canonical_bytes(self.record.to_dict()) != self.record_bytes
            or canonical_bytes(self.result.to_dict()) != self.result_bytes
            or canonical_bytes(self.projected.to_dict()) != self.projected_bytes
        ):
            raise ReviewInputError("ordinary publication source content changed")

    def qualify(
        self,
        *,
        record,
        result,
        projected_result,
        assembled,
        eligibility,
        checks_enabled,
        cleanup_targets=(),
        grouped=False,
    ):
        self._require(self.journal, self.publisher)
        journal = self.journal
        from .publication import review_result_digest

        if (
            self in _ISSUED_WITNESS
            or record is not journal._record
            or not isinstance(result, ReviewResult)
            or not isinstance(projected_result, ReviewResult)
            or result.transaction is None
            or record.transaction is None
            or not result.transaction.logical_identity_matches(record.transaction)
            or result.content_digest() != record.transaction.result_sha256
            or cleanup_targets
            or grouped
            or type(checks_enabled) is not bool
            or record.quarantine_lineage is not None
            or record.historical_assessments is not None
            or eligibility.result_digest != review_result_digest(projected_result)
            or eligibility.head_sha != journal.head_sha
            or eligibility.partitioned_root is not None
            or eligibility.native_union is not None
            or eligibility.human_inventory_reference is not None
            or eligibility.historical_assessments is not None
            or eligibility.quarantine_assessments is not None
            or any(effect["status"] == "pending" for effect in journal.effects)
            or "pr" not in _OBSERVED[self]
            or "reviews" not in _OBSERVED[self]
        ):
            raise ReviewInputError("ordinary_publication_read_tail_unqualified")
        from review_sensei.finding_lifecycle import decode_lifecycle_state

        _require_reader_profile(self.publisher)
        if eligibility.facts.provider == "openrouter":
            raise ReviewInputError(
                "ordinary publication extended reader is unqualified"
            )
        state = decode_lifecycle_state(journal._state()).state
        inventory = getattr(state, "inventory", None) or getattr(
            state, "authoritative_decision_inventory", None
        )
        populated = bool(result.comments)
        if populated and (
            not 0 < len(result.comments) <= 2
            or getattr(
                state,
                "max_active_findings",
                getattr(
                    getattr(state, "display_active_findings", None),
                    "max_active_findings",
                    None,
                ),
            )
            != 1
            or inventory is None
            or len(inventory.obligations) != len(result.comments)
            or not unbound_population(journal, result, state)
            or journal.reviewer.native_finding_receipts(
                repository_id=journal.identity.repository_id,
                pull_request=journal.identity.pull_request,
            )
            or getattr(journal.reviewer, "_native_finding_context", {}).get(
                "existing_bindings"
            )
        ):
            raise ReviewInputError(
                "ordinary publication retained native route is unqualified"
            )
        if not populated and inventory is not None and inventory.obligations:
            raise ReviewInputError("ordinary_publication_read_tail_nonempty_inventory")
        self.record, self.record_bytes = record, canonical_bytes(record.to_dict())
        self.result, self.result_bytes = result, canonical_bytes(result.to_dict())
        self.projected, self.projected_bytes = (
            projected_result,
            canonical_bytes(projected_result.to_dict()),
        )
        _QUALIFIED[self] = (
            self.record,
            self.record_bytes,
            self.result,
            self.result_bytes,
            self.projected,
            self.projected_bytes,
        )
        roles = (
            tuple(
                (
                    role,
                    4
                    if role == "threads"
                    else 3
                    if role == "review_comments"
                    else calls,
                )
                for role, calls in _ROLES
            )
            if populated
            else _ROLES
        )
        if populated and assembled.body_explanations:
            roles = (*roles, ("review", 1))
        _REMAINING[self] = dict(roles)
        self.witness = OrdinaryPublicationReadWitness(
            roles, sum(calls for _, calls in roles)
        )
        _WITNESSES[self.witness] = self
        _WITNESS_SHAPES[self.witness] = (
            self.witness.read_roles,
            self.witness.read_calls,
        )
        _ISSUED_WITNESS[self] = self.witness
        return self.witness

    def role(self, method, path, body):
        scope = self.journal.identity
        prefix = f"/repos/{scope.repository}"
        parsed = urlsplit(path)
        if method == "GET":
            if parsed.path == f"{prefix}/pulls/{scope.pull_request}":
                return "pr"
            if parsed.path == f"{prefix}/pulls/{scope.pull_request}/reviews":
                return "reviews"
            if re.fullmatch(
                re.escape(f"{prefix}/pulls/{scope.pull_request}/reviews/")
                + r"[1-9][0-9]*",
                parsed.path,
            ):
                return "review"
            if re.fullmatch(
                re.escape(f"{prefix}/pulls/{scope.pull_request}/reviews/")
                + r"[1-9][0-9]*/comments",
                parsed.path,
            ):
                return "review_comments"
            if parsed.path == f"{prefix}/commits/{self.journal.head_sha}/check-runs":
                return "checks"
            if (
                parsed.path.startswith(prefix + "/check-runs/")
                and parsed.path.rsplit("/", 1)[1].isdigit()
            ):
                return "check"
        if (
            method == "POST"
            and parsed.path == "/graphql"
            and isinstance(body, dict)
            and str(body.get("query", "")).lstrip().startswith("query")
        ):
            variables = body.get("variables", {})
            owner, name = scope.repository.split("/", 1)
            if (
                variables.get("owner"),
                variables.get("name"),
                variables.get("number"),
            ) != (owner, name, scope.pull_request) or variables.get(
                "after"
            ) is not None:
                if _REMAINING.get(self) is not None:
                    raise ReviewInputError(
                        "ordinary publication thread query scope changed"
                    )
                return None
            return "threads"
        return None

    def read(self, transport, method, path, kwargs):
        self._require(self.journal, self.publisher)
        if not any(transport is old for _, _, old, _, _ in self.bindings):
            raise ReviewInputError("ordinary publication read transport is unbound")
        body = kwargs.get("body")
        is_read = method == "GET" or (
            method == "POST"
            and path == "/graphql"
            and isinstance(body, dict)
            and str(body.get("query", "")).lstrip().startswith("query")
        )
        role = self.role(method, path, body) if is_read else None
        if _REMAINING.get(self) is not None and is_read:
            query = parse_qs(urlsplit(path).query)
            if (
                role is None
                or _REMAINING[self].get(role, 0) <= 0
                or query.get("page", ["1"]) != ["1"]
                or query.get("per_page", ["100"]) != ["100"]
            ):
                raise ReviewInputError("ordinary publication read exceeds source tail")
            _REMAINING[self][role] -= 1
        try:
            response = transport.request(method, path, **kwargs)
        except GitHubHTTPError as error:
            if _REMAINING.get(self) is not None and is_read:
                raise ReviewInputError(
                    "ordinary publication bounded read failed without retry"
                ) from error
            raise
        status, value = response
        complete = status == 200
        if role in {"reviews", "review_comments"}:
            query = parse_qs(urlsplit(path).query)
            complete = (
                complete
                and isinstance(value, list)
                and len(value) < 100
                and query.get("page", ["1"]) == ["1"]
                and query.get("per_page", ["100"]) == ["100"]
            )
        elif role == "checks":
            complete = (
                complete
                and isinstance(value, dict)
                and isinstance(value.get("check_runs"), list)
                and len(value["check_runs"]) < 100
                and type(value.get("total_count", len(value["check_runs"]))) is int
                and value.get("total_count", len(value["check_runs"]))
                == len(value["check_runs"])
            )
        elif role == "threads":

            def closed(item):
                if isinstance(item, dict):
                    if "pageInfo" in item and (
                        not isinstance(item["pageInfo"], dict)
                        or item["pageInfo"].get("hasNextPage") is not False
                    ):
                        return False
                    return all(closed(child) for child in item.values())
                return not isinstance(item, list) or all(
                    closed(child) for child in item
                )

            complete = (
                complete
                and isinstance(value, dict)
                and value.get("errors") in (None, [])
                and closed(value)
            )
        elif role in {"pr", "check"}:
            complete = complete and isinstance(value, dict)
        if role is not None:
            # A definitive check-lookup denial is handled by the original
            # check publisher and its physically bound rejection receipt. It
            # is not a successful census or native-effect settlement, and its
            # one charged read still consumes this role. Other incomplete
            # responses retain the original hard stop.
            known_check_denial = (
                role == "checks" and type(status) is int and status in {401, 403}
            )
            if (
                _REMAINING.get(self) is not None
                and not complete
                and not known_check_denial
            ):
                raise ReviewInputError("ordinary publication read is incomplete")
            if complete:
                _OBSERVED[self][role] = canonical_bytes(value)
            else:
                _OBSERVED[self].pop(role, None)
        return response


class _ClosedPublicationHttp(GitHubHttp):
    def __init__(self, observation, transport):
        super().__init__(api_url=transport.api_url, timeout=transport.timeout)
        self.observation, self.transport = observation, transport
        self.primed_issue_comments = transport.primed_issue_comments

    def __getattr__(self, name):
        return getattr(self.transport, name)

    @property
    def preflight_native_batch(self):
        return getattr(self.transport, "preflight_native_batch")

    def request(self, method, path, **kwargs):
        if type(self.observation) is not _Observation:
            raise ReviewInputError("ordinary publication observer changed")
        return _Observation.read(self.observation, self.transport, method, path, kwargs)


_WRAPPER_REQUEST = _ClosedPublicationHttp.request
_OBSERVATION_METHODS = {
    name: method for name, method in vars(_Observation).items() if callable(method)
}


@contextmanager
def observe_publication(publisher, journal):
    owners = _source_guard(publisher, journal)
    budget = (
        journal._native_budget
        or publisher.finalizer.evidence_budget
        or publisher.finalizer._operation_budget
    )
    if budget is None:
        raise ReviewInputError("ordinary publication read budget is absent")
    observation = _Observation(publisher, journal, budget)
    for owner, attr in [(owner, "http") for owner in owners] + [
        (journal, "_readback_http")
    ]:
        old = getattr(owner, attr)
        signature = _transport_signature(old, journal, budget)
        wrapper = _ClosedPublicationHttp(observation, old)
        observation.bindings.append((owner, attr, old, wrapper, signature))
    _ACTIVE[observation] = (observation.scope, tuple(observation.bindings))
    try:
        for owner, attr, old, wrapper, _ in observation.bindings:
            setattr(owner, attr, wrapper)
        yield observation
    finally:
        _ACTIVE.pop(observation, None)
        _OBSERVED.pop(observation, None)
        _REMAINING.pop(observation, None)
        _QUALIFIED.pop(observation, None)
        issued = _ISSUED_WITNESS.pop(observation, None)
        if issued is not None:
            _WITNESSES.pop(issued, None)
            _WITNESS_SHAPES.pop(issued, None)
        for owner, attr, old, wrapper, _ in reversed(observation.bindings):
            setattr(owner, attr, old)
