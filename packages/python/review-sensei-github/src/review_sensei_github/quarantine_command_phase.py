"""Original cold mutation classification, never a capacity or execution grant.

This seam distinguishes the physical mutation families before caller debit.
A future admitted suffix must additionally bind their exact order, successor
roots, bounded readers and every remaining operation allocation. Merely obtaining
one of these descriptions cannot enable either a reader or a writer.
"""

import hashlib
import inspect
import json
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


@dataclass(frozen=True)
class MutationDescription:
    kind: str
    method: str
    path: str
    body_sha256: str
    root: str


def _optional_frame(module, function):
    require_module_seal(module)
    code = inspect.unwrap(function).__code__
    frame = inspect.currentframe()
    while frame is not None:
        if frame.f_code is code and frame.f_globals is vars(module):
            return frame
        frame = frame.f_back
    return None


def classify_original_mutation(caller):
    """Describe an actual original pre-debit mutation, or reject its source.

    Reads/capability acquisition return None and retain their existing charging.
    This function has no registry, installation hook, I/O or admission side effect.
    """
    from . import api_run_requests, broker_client, quarantine_census
    from .quarantine_command_execution import _source_frame

    require_module_seal(sys.modules[__name__])
    frame = _source_frame(
        api_run_requests, api_run_requests.ApiRunRequestTransport.open
    )
    if (
        type(caller) is not api_run_requests.ApiRunRequestTransport
        or frame.f_locals.get("self") is not caller
        or "deadline" not in frame.f_locals
        or "pending" in frame.f_locals
    ):
        raise ReviewInputError("cold mutation classification is not pre-debit")
    record, root = caller._effect_record, caller._root
    if record is None or root is None:
        raise ReviewInputError("cold mutation lacks its current original root")
    request = frame.f_locals["request"]
    method = request.get_method()
    if method in {"GET", "HEAD"}:
        return None
    require_module_seal(quarantine_census)
    origin = caller._quarantine_census_api_url
    if origin is not None and quarantine_census.is_readonly_census(
        request, origin, identity=record.identity()
    ):
        return None
    broker = _optional_frame(broker_client, broker_client.BrokerClient.exchange)
    if broker is not None:
        client = broker.f_locals["self"]
        post = _optional_frame(broker_client, broker_client.BrokerClient._post)
        require_instance_source(client, methods=("exchange", "_post"))
        if (
            post is None
            or post.f_locals.get("self") is not client
            or post.f_locals.get("request") is not request
            or request.data != post.f_locals.get("body")
            or request.full_url != (post.f_locals.get("url") or client.broker_url)
        ):
            raise ReviewInputError("cold capability request source changed")
        # The independently priced capability request carries credentials. Do
        # not copy, hash or expose its request body in a mutation description.
        return None
    from . import action_receipt, publication, quarantine_action, quarantine_checkpoint

    parsed = urlsplit(request.full_url)
    if parsed.query or parsed.fragment:
        raise ReviewInputError("cold native mutation has unexpected URL framing")
    try:
        body = json.loads(request.data)
    except (TypeError, ValueError) as error:
        raise ReviewInputError("cold native mutation body is unsupported") from error
    kind, api_url = None, None
    checkpoint = _optional_frame(
        quarantine_checkpoint, quarantine_checkpoint.replace_quarantine_checkpoint
    )
    if checkpoint is not None:
        snapshot = checkpoint.f_locals.get("snapshot")
        if snapshot is None or snapshot.caller is not caller:
            raise ReviewInputError("cold checkpoint mutation source changed")
        snapshot.require()
        expected = snapshot.ledger.http.repository_path(
            snapshot.identity.repository, f"/issues/comments/{snapshot.comment_id}"
        )
        if (
            method != "PATCH"
            or parsed.path != expected
            or body != {"body": snapshot.candidate_body}
            or snapshot.candidate is not caller._checkpoint
            or snapshot.root != root
        ):
            raise ReviewInputError("cold checkpoint mutation candidate changed")
        if (
            caller._terminal_reason is not None
            and _optional_frame(
                api_run_requests, api_run_requests.ApiRunRequestTransport.close_action
            )
            is None
        ):
            raise ReviewInputError("cold terminal root source changed")
        api_url = snapshot.ledger.http.api_url
        kind = "terminal-root" if caller._terminal_reason is not None else "root"
    event = _optional_frame(quarantine_action, quarantine_action._stage_decision_event)
    if kind is None and event is not None:
        run = event.f_locals.get("run")
        if (
            run is None
            or run.caller is not caller
            or method != "POST"
            or parsed.path != run.ledger._comments_path(run.identity)
            or body != {"body": event.f_locals.get("body")}
        ):
            raise ReviewInputError("cold event mutation source changed")
        api_url = run.http.api_url
        kind = "event"
    finalizer = _optional_frame(
        publication,
        publication.ReviewApprovalFinalizer._refinalize_with_dispositions_impl,
    )
    if kind is None and finalizer is not None:
        values = finalizer.f_locals
        journal = values.get("journal")
        chosen = values.get("chosen")
        if (
            journal is None
            or journal.ledger.caller is not caller
            or not isinstance(chosen, dict)
            or method != "PUT"
            or parsed.path
            != journal.ledger.http.repository_path(
                values["repository"],
                f"/pulls/{values['pull_request']}/reviews/{chosen['id']}",
            )
            or body != {"body": values.get("new_body")}
        ):
            raise ReviewInputError("cold native review mutation source changed")
        api_url = journal.ledger.http.api_url
        kind = "review"
    receipt = _optional_frame(action_receipt, action_receipt.publish_command_receipt)
    if kind is None and receipt is not None:
        values = receipt.f_locals
        run = values.get("run")
        if (
            run is None
            or run.caller is not caller
            or method != ("PATCH" if values.get("previous") else "POST")
            or parsed.path != values.get("target")
            or body != {"body": values.get("text")}
        ):
            raise ReviewInputError("cold receipt mutation source changed")
        api_url = run.http.api_url
        kind = "receipt"
    if kind is None:
        raise ReviewInputError("cold suffix mutation family is unsupported")
    if api_url is None or (parsed.scheme, parsed.netloc) != (
        urlsplit(api_url).scheme,
        urlsplit(api_url).netloc,
    ):
        raise ReviewInputError("cold native mutation origin changed")
    return MutationDescription(
        kind,
        method,
        parsed.path,
        hashlib.sha256(canonical_bytes(body)).hexdigest(),
        root,
    )


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
