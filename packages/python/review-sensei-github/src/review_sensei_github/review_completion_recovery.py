"""Authenticate an existing review's complete terminal tail without writes."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, NoReturn

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

from .approval import approval_facts_from_result, evaluate_approval_facts
from .checks import (
    REVIEW_CHECK_EXTERNAL_ID,
    REVIEW_CHECK_NAME,
    check_outcome_for_result,
)
from .http import GitHubHttp
from .publication import (
    FINDING_INDEX_MARKER_PREFIX,
    HISTORY_PAGE_SIZES,
    MAX_PAGINATION_PAGES,
    PUBLISHED_REVIEW_STATES,
    REVIEW_MARKER_PREFIX,
    approval_eligibility_from_body,
    approval_marker,
    finding_instance_from_body,
    finding_publication_scope,
    parse_finding_index,
    review_result_digest,
)


def _refuse(reason: str) -> NoReturn:
    raise ReviewInputError("successor publication tail is unverified: " + reason)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class _ReadOnlyHttp(GitHubHttp):
    def __init__(self, transport: GitHubHttp) -> None:
        super().__init__(api_url=transport.api_url, timeout=transport.timeout)
        self.transport = transport

    def request(self, method, path, *, token, body=None, timeout_seconds=None):
        if method != "GET" and not (
            method == "POST"
            and path == "/graphql"
            and isinstance(body, dict)
            and body.get("operationName")
            in {"ReviewCompletionThreads", "ReviewCompletionReviewDates"}
            and isinstance(body.get("query"), str)
            and body["query"].startswith("query ")
        ):
            _refuse("completion authentication is read-only")
        return self.transport.request(
            method, path, token=token, body=body, timeout_seconds=timeout_seconds
        )


def _get(http, path, token):
    status, value = http.request("GET", path, token=token)
    if status != 200 or not isinstance(value, dict):
        _refuse("required native evidence is unavailable")
    return value


def _stamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        _refuse("historical edit time is unavailable")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _refuse("historical edit time is invalid")


def _owned(value, producer, app_slug):
    user = value.get("user") if isinstance(value, dict) else None
    return (
        isinstance(user, dict)
        and user.get("id") == producer
        and user.get("type") == "Bot"
        and isinstance(user.get("login"), str)
        and user["login"].casefold() == app_slug.casefold()
    )


def _threads(http, token, identity):
    query = (
        "query ReviewCompletionThreads($owner:String!,$name:String!,$number:Int!,$after:String){"
        "repository(owner:$owner,name:$name){pullRequest(number:$number){"
        "headRefOid baseRefOid reviewThreads(first:100,after:$after){"
        "pageInfo{hasNextPage endCursor}nodes{id isResolved comments(first:1){nodes{"
        "databaseId body createdAt updatedAt author{login __typename}}}}}}}}"
    )
    owner, name = identity.repository.split("/", 1)
    after = None
    nodes = []
    seen = set()
    for _ in range(MAX_PAGINATION_PAGES):
        status, payload = http.request(
            "POST",
            "/graphql",
            token=token,
            body={
                "operationName": "ReviewCompletionThreads",
                "query": query,
                "variables": {
                    "owner": owner,
                    "name": name,
                    "number": identity.pull_request,
                    "after": after,
                },
            },
        )
        if status != 200 or not isinstance(payload, dict) or payload.get("errors"):
            _refuse("complete cleanup inventory is unavailable")
        try:
            pull = payload["data"]["repository"]["pullRequest"]
            connection = pull["reviewThreads"]
            page, info = connection["nodes"], connection["pageInfo"]
        except (KeyError, TypeError):
            _refuse("complete cleanup inventory is invalid")
        if not isinstance(page, list) or len(page) > 100 or not isinstance(info, dict):
            _refuse("complete cleanup pagination is invalid")
        if not isinstance(pull.get("headRefOid"), str) or not isinstance(
            pull.get("baseRefOid"), str
        ):
            _refuse("thread inventory snapshot is unavailable")
        for node in page:
            if (
                not isinstance(node, dict)
                or not isinstance(node.get("id"), str)
                or node["id"] in seen
            ):
                _refuse("cleanup thread identity conflicts")
            seen.add(node["id"])
            nodes.append(node)
        if info.get("hasNextPage") is False:
            return pull["headRefOid"], pull["baseRefOid"], nodes
        cursor = info.get("endCursor")
        if (
            info.get("hasNextPage") is not True
            or not isinstance(cursor, str)
            or cursor == after
        ):
            _refuse("complete cleanup pagination is unavailable")
        after = cursor
    _refuse("cleanup pagination exceeds its existing bound")


def _review_dates(http, token, reviews):
    if (
        not reviews
        or len(reviews) > 100
        or any(not isinstance(r.get("node_id"), str) for r in reviews)
        or any(type(r.get("id")) is not int or r["id"] <= 0 for r in reviews)
        or len({r["id"] for r in reviews}) != len(reviews)
        or len({r["node_id"] for r in reviews}) != len(reviews)
    ):
        _refuse("historical review identities are unavailable")
    query = (
        "query ReviewCompletionReviewDates($ids:[ID!]!){nodes(ids:$ids){"
        "...on PullRequestReview{databaseId createdAt updatedAt lastEditedAt commit{oid}}}}"
    )
    status, payload = http.request(
        "POST",
        "/graphql",
        token=token,
        body={
            "operationName": "ReviewCompletionReviewDates",
            "query": query,
            "variables": {"ids": [r["node_id"] for r in reviews]},
        },
    )
    try:
        nodes = payload["data"]["nodes"]
    except (KeyError, TypeError):
        _refuse("historical review edit evidence is unavailable")
    if (
        status != 200
        or payload.get("errors")
        or not isinstance(nodes, list)
        or len(nodes) != len(reviews)
    ):
        _refuse("historical review edit inventory is incomplete")
    dates = {}
    for node, review in zip(nodes, reviews, strict=True):
        if (
            not isinstance(node, dict)
            or node.get("databaseId") != review["id"]
            or node.get("commit", {}).get("oid") != review["commit_id"]
            or "lastEditedAt" not in node
        ):
            _refuse("historical review edit identity conflicts")
        if _stamp(node.get("createdAt")) > _stamp(node.get("updatedAt")):
            _refuse("historical review edit chronology conflicts")
        if node["lastEditedAt"] is not None:
            _stamp(node["lastEditedAt"])
        dates[review["id"]] = node
    return dates


def _check(http, token, identity, head, expected, app_slug):
    path = http.repository_path(identity.repository, f"/commits/{head}/check-runs")
    found = []
    seen = set()
    total = None
    for page in range(1, MAX_PAGINATION_PAGES + 1):
        value = _get(
            http,
            path + f"?check_name={REVIEW_CHECK_NAME}&per_page=100&page={page}",
            token,
        )
        rows, count = value.get("check_runs"), value.get("total_count")
        if (
            not isinstance(rows, list)
            or len(rows) > 100
            or type(count) is not int
            or count < 0
            or total is not None
            and total != count
        ):
            _refuse("required check inventory is incomplete")
        total = count
        for row in rows:
            if (
                not isinstance(row, dict)
                or type(row.get("id")) is not int
                or row["id"] in seen
            ):
                _refuse("required check identity conflicts")
            seen.add(row["id"])
            app = row.get("app")
            if (
                row.get("name") == REVIEW_CHECK_NAME
                and isinstance(app, dict)
                and app.get("slug") == app_slug.removesuffix("[bot]")
            ):
                found.append(row)
        if len(rows) < 100:
            if len(seen) != total:
                _refuse("required check pagination is incomplete")
            break
    else:
        _refuse("required check pagination exceeds its existing bound")
    if len(found) != 1:
        _refuse("required check is missing or ambiguous")
    check = found[0]
    if (
        check.get("head_sha") != head
        or check.get("status") != "completed"
        or check.get("conclusion") != expected.conclusion
        or check.get("external_id") != REVIEW_CHECK_EXTERNAL_ID
        or type(check["app"].get("id")) is not int
        or check["app"]["id"] <= 0
        or check.get("output", {}).get("title") != expected.title
        or check.get("output", {}).get("summary") != expected.summary
    ):
        _refuse("required check does not confirm this result")
    return check


def _confirm_native_effects(
    *,
    run,
    analysis,
    context,
    store,
    result,
    assembled,
    publisher,
    http,
    token,
    identity,
    pull,
    current_base,
    current_head,
):
    reviews = http.paginate(
        path=http.repository_path(
            identity.repository, f"/pulls/{identity.pull_request}/reviews"
        ),
        token=token,
        page_sizes=HISTORY_PAGE_SIZES,
        max_requests=MAX_PAGINATION_PAGES,
    )
    owned_reviews = [
        r
        for r in reviews
        if _owned(r, context.producer_id, context.app_slug)
        and isinstance(r.get("body"), str)
    ]
    finding_reviews = [
        r
        for r in owned_reviews
        if FINDING_INDEX_MARKER_PREFIX in r["body"] or REVIEW_MARKER_PREFIX in r["body"]
    ]
    selected = []
    for review in finding_reviews:
        index = parse_finding_index(
            review["body"],
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
            head_sha=review.get("commit_id"),
        )
        if index is None:
            _refuse("historical finding inventory is unverifiable")
        if (
            review.get("state") in PUBLISHED_REVIEW_STATES
            and review.get("commit_id") == run.head_sha
            and index.result_digest == review_result_digest(result)
        ):
            selected.append(review)
    if len(selected) != 1:
        _refuse("exact native finding review is missing or ambiguous")
    review = selected[0]
    publisher._verify_complete_review(
        token=token,
        repository=identity.repository,
        repository_id=identity.repository_id,
        pull_request=identity.pull_request,
        head_sha=run.head_sha,
        app_slug=context.app_slug,
        review=review,
        assembled=assembled,
        base_sha=run.base_sha,
        result_digest=review_result_digest(result),
    )
    # The reader below is bound to this exact independently authenticated store.
    from review_sensei.human_inventory import inventory_from_result_document

    def read_human(reference):
        if reference != store.authenticated_reference:
            _refuse("native admission reference changed")
        document = store.read_authenticated_snapshot(reference)
        return inventory_from_result_document(
            document["human_inventory"],
            result_digest=context.publication_digest,
            base_sha=run.base_sha,
        )

    eligibility = approval_eligibility_from_body(
        review["body"], human_inventory_reader=read_human
    )
    if (
        eligibility is None
        or eligibility.head_sha != run.head_sha
        or eligibility.result_digest != review_result_digest(result)
    ):
        _refuse("native eligibility is unavailable")
    expected_facts = approval_facts_from_result(
        result,
        enabled=run.plan.policy.reviews == "auto-approve"
        and analysis.policy.automatic_github_review_events,
        app_authored=pull["user"]["login"] == context.app_slug,
        qualification=eligibility.facts.qualification,
        check_published=True,
        has_open_review_threads=eligibility.facts.has_open_review_threads,
    )
    from review_sensei.human_assessment import PendingHumanReview

    if (
        eligibility.facts != expected_facts
        or eligibility.human_review
        != PendingHumanReview.from_result(result, run.base_sha)
    ):
        _refuse("native eligibility differs from immutable admission")
    expected = check_outcome_for_result(
        result,
        policy=run.plan.policy.reviews,
    )
    check = _check(http, token, identity, run.head_sha, expected, context.app_slug)
    if evaluate_approval_facts(eligibility.facts).approved:
        marker = approval_marker(
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
            head_sha=run.head_sha,
            base_sha=run.base_sha,
        )
        if not any(
            _owned(r, context.producer_id, context.app_slug)
            and r.get("commit_id") == run.head_sha
            and r.get("state") == "APPROVED"
            and r.get("body") == marker
            for r in reviews
        ):
            _refuse("required native approval is not confirmed")
    dates = _review_dates(http, token, owned_reviews)
    head, base, threads = _threads(http, token, identity)
    if (head, base) != (current_head, current_base):
        _refuse("thread inventory head or base changed")
    roots = {}
    for thread in threads:
        try:
            root = thread["comments"]["nodes"][0]
            identifier = root["databaseId"]
        except (KeyError, IndexError, TypeError):
            _refuse("thread root inventory is incomplete")
        if (
            type(identifier) is not int
            or identifier <= 0
            or identifier in roots
            or type(thread.get("isResolved")) is not bool
            or len(thread["comments"]["nodes"]) != 1
        ):
            _refuse("thread root identity conflicts")
        roots[identifier] = (thread, root)
    inline = http.paginate(
        path=http.repository_path(
            identity.repository, f"/pulls/{identity.pull_request}/comments"
        ),
        token=token,
        page_sizes=HISTORY_PAGE_SIZES,
        max_requests=MAX_PAGINATION_PAGES,
    )
    rest_roots = {
        c["id"]: c
        for c in inline
        if isinstance(c, dict)
        and not c.get("in_reply_to_id")
        and _owned(c, context.producer_id, context.app_slug)
    }
    concerns = {item[1] for item in assembled.inline_comments}
    targets = {}
    submitted = _stamp(review.get("submitted_at"))
    for old in owned_reviews:
        edited = dates[old["id"]].get("lastEditedAt")
        if edited is not None and _stamp(edited) > submitted:
            _refuse("original cleanup inventory was edited after publication")
    accounted_old_roots = set()
    for old in finding_reviews:
        if old["commit_id"] == run.head_sha:
            continue
        index = parse_finding_index(
            old["body"],
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
            head_sha=old["commit_id"],
        )
        assert index is not None
        for entry in index.entries:
            if entry.body_placed or entry.lifecycle not in concerns:
                continue
            candidates = [
                c
                for c in rest_roots.values()
                if c.get("pull_request_review_id") == old["id"]
                and finding_instance_from_body(c.get("body")) == entry.instance
            ]
            if len(candidates) != 1 or candidates[0]["id"] not in roots:
                _refuse("original cleanup target is missing or ambiguous")
            comment = candidates[0]
            thread, root = roots[comment["id"]]
            author = root.get("author") or {}
            scope = finding_publication_scope(comment.get("body"))
            if (
                scope is None
                or scope[:2] != (identity.repository_id, identity.pull_request)
                or scope[2] != old["commit_id"]
                or root.get("body") != comment["body"]
                or author.get("__typename") != "Bot"
                or not isinstance(author.get("login"), str)
                or author["login"].casefold().removesuffix("[bot]")
                != context.app_slug.casefold().removesuffix("[bot]")
                or root.get("createdAt") != comment.get("created_at")
                or root.get("updatedAt") != comment.get("updated_at")
                or _stamp(comment.get("created_at")) > submitted
                or _stamp(comment.get("updated_at")) > submitted
            ):
                _refuse("original cleanup target identity changed")
            accounted_old_roots.add(comment["id"])
            targets[thread["id"]] = {
                "root": comment["id"],
                "instance": entry.instance,
                "concern": entry.lifecycle,
                "resolved": thread["isResolved"],
                "body_sha256": hashlib.sha256(comment["body"].encode()).hexdigest(),
            }
    # Reuse the original selector over every root as an independent completeness
    # cross-check; an old root absent from native indexes cannot disappear.
    from .publication import finding_fingerprint_from_body

    for identifier, (thread, root) in roots.items():
        scope = finding_publication_scope(root.get("body"))
        author = root.get("author") or {}
        if (
            author.get("__typename") == "Bot"
            and author.get("login", "").removesuffix("[bot]").casefold()
            == context.app_slug.removesuffix("[bot]").casefold()
            and scope is not None
            and scope[:2] == (identity.repository_id, identity.pull_request)
            and scope[2] != run.head_sha
            and finding_fingerprint_from_body(root["body"]) in concerns
            and identifier not in accounted_old_roots
        ):
            _refuse("original cleanup target set is unprovable")
    if any(not target["resolved"] for target in targets.values()):
        _refuse("a superseded cleanup target remains unresolved")
    # Fresh closing reads are observations, never writes or refunds. Unchanged
    # complete target inventory is required before a process-issued witness.
    closing = _threads(http, token, identity)
    if closing != (head, base, threads):
        _refuse("cleanup inventory changed during authentication")
    check_again = _check(
        http, token, identity, run.head_sha, expected, context.app_slug
    )
    if check_again != check:
        _refuse("required check changed during authentication")
    observed_review = _get(
        http,
        http.repository_path(
            identity.repository,
            f"/pulls/{identity.pull_request}/reviews/{review['id']}",
        ),
        token,
    )
    if observed_review != review:
        _refuse("native review changed during authentication")
    closing_reviews = http.paginate(
        path=http.repository_path(
            identity.repository, f"/pulls/{identity.pull_request}/reviews"
        ),
        token=token,
        page_sizes=HISTORY_PAGE_SIZES,
        max_requests=MAX_PAGINATION_PAGES,
    )
    if closing_reviews != reviews or _review_dates(http, token, owned_reviews) != dates:
        _refuse("historical review inventory changed during authentication")
    return {
        "review_id": review["id"],
        "check_id": check["id"],
        "check_conclusion": check["conclusion"],
        "target_inventory_sha256": _digest(targets),
    }
