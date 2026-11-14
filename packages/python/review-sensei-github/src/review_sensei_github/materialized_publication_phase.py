"""Source-owned hot materialization phase; no smaller census price is enabled.

The installed source wrappers record original completed computation/retention.
Once a qualifying census opens, operation-comment writes are forbidden, including
identical-payload retries to fresh IDs. Readback and the original journal/root
writers remain unchanged. No public receipt or observation can enter the phase.
"""

from __future__ import annotations

import inspect
import sys
from contextvars import ContextVar
from functools import wraps
from typing import Any
from weakref import WeakKeyDictionary, WeakSet

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


def _build():
    accepted: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    active: ContextVar[Any] = ContextVar(
        "source_materialized_publication", default=None
    )
    analyzing: ContextVar[Any] = ContextVar(
        "source_accepted_materialization", default=None
    )
    installed = set()
    source_classes: dict[type, Any] = {}
    port_instances: WeakSet[Any] = WeakSet()
    original_functions: list[tuple[Any, Any]] = []
    journal_checkpoint_sources: dict[type, Any] = {}
    publication_sources: dict[type, Any] = {}

    def definition(name):
        frame = inspect.currentframe()
        frame = frame.f_back if frame else None
        frame = frame.f_back if frame else None
        module = sys.modules.get(name)
        spec = getattr(module, "__spec__", None)
        loader = getattr(spec, "loader", None)
        get_code = getattr(loader, "get_code", None)
        if (
            frame is None
            or module is None
            or name in installed
            or frame.f_code.co_name != "<module>"
            or frame.f_globals is not vars(module)
            or not getattr(spec, "_initializing", False)
            or not callable(get_code)
            or get_code(name) != frame.f_code
        ):
            raise ReviewInputError(
                "materialization source installer is not defining source"
            )
        installed.add(name)

    def transport(http):
        from .publication_recovery import _publication_transport

        return _publication_transport(http)

    def caller_for(http):
        from .caller_operation import caller_for_http

        return caller_for_http(http)

    def class_seal(cls):
        return tuple(
            (name, value, getattr(value, "__code__", None))
            for name, value in vars(cls).items()
            if callable(value) or isinstance(value, property)
        )

    def ports_intact():
        if any(function.__code__ is not code for function, code in original_functions):
            raise ReviewInputError("materialized publication original writer changed")
        if not source_classes or any(
            class_seal(cls) != seal for cls, seal in source_classes.items()
        ):
            raise ReviewInputError("materialized publication writer source changed")
        for instance in tuple(port_instances):
            if not class_intact(instance):
                raise ReviewInputError(
                    "materialized publication writer instance changed"
                )

    def class_intact(instance):
        cls = type(instance)
        seal = source_classes.get(cls)
        return (
            seal is not None
            and class_seal(cls) == seal
            and not any(name in vars(instance) for name, _, _ in seal)
        )

    def install_analysis(namespace):
        definition("review_sensei_github.operation_entry")
        original = namespace["run_actions_analysis"]
        original_code = original.__code__
        from . import accepted_result

        remember = accepted_result.remember_accepted_result
        remember_code = remember.__code__

        @wraps(original)
        def run(**kwargs):
            http = kwargs["http"]
            key = transport(http)
            accepted.pop(key, None)
            capture = {
                "transport": key,
                "bodies": {},
                "reads": {},
                "closed": False,
                "poisoned": False,
            }
            token = analyzing.set(capture)
            try:
                result = original(**kwargs)
            finally:
                analyzing.reset(token)
            if (
                not capture["poisoned"]
                and original.__code__ is original_code
                and namespace["run_actions_analysis"] is run
                and accepted_result.remember_accepted_result is remember
                and remember.__code__ is remember_code
                and getattr(result, "fresh", None) is not None
                and getattr(result, "operation", None) is not None
            ):
                captured = accepted_result._CAPTURED.get(key)
                caller = caller_for(http)
                if captured is not None and caller is not None:
                    accepted[key] = (
                        caller,
                        captured,
                        canonical_bytes(captured.reference),
                        tuple(sorted(capture["bodies"].items())),
                        tuple(sorted(capture["reads"].items())),
                    )
            return result

        namespace["run_actions_analysis"] = run

    def install_application(cls):
        definition("review_sensei_github.application")
        original = cls.publish_review
        body = original
        while hasattr(body, "__wrapped__"):
            body = body.__wrapped__
        application_code = body.__code__

        @wraps(original)
        def publish(app, **kwargs):
            ledger = app.session_ledger
            caller = getattr(ledger, "caller", None)
            http = getattr(ledger, "http", None)
            key = transport(http) if http is not None else None
            selected = accepted.get(key) if key is not None else None
            entry = None
            if (
                type(app) is cls
                and selected is not None
                and selected[0] is caller
                and getattr(ledger, "_quarantine_required", False)
                and getattr(key, "_quarantine_capacity_guard", None) is None
            ):
                entry = {
                    "app": app,
                    "application_code": application_code,
                    "journal": None,
                    "ledger": ledger,
                    "caller": caller,
                    "transport": key,
                    "accepted": selected,
                    "retained": None,
                    "reads": {},
                    "bodies": {},
                    "poisoned": False,
                    "closed": False,
                    "censuses": 0,
                    "write_frames": [],
                    "projection": None,
                    "projection_error": None,
                }
            token = active.set(entry)
            try:
                return original(app, **kwargs)
            finally:
                active.reset(token)
                if entry is not None:
                    entry["write_frames"].clear()
                if key is not None:
                    accepted.pop(key, None)

        cls.publish_review = publish

    def install_journal(cls):
        definition("review_sensei_github.finding_lifecycle")
        original = cls.__init__
        checkpoint = cls.checkpoint
        if hasattr(checkpoint, "__wrapped__"):
            raise ReviewInputError("materialization checkpoint is not defining source")
        journal_checkpoint_sources[cls] = checkpoint.__code__
        original_functions.append((checkpoint, checkpoint.__code__))

        @wraps(original)
        def construct(journal, *args, **kwargs):
            original(journal, *args, **kwargs)
            entry = active.get()
            frame = inspect.currentframe()
            caller = frame.f_back if frame else None
            if (
                entry is not None
                and caller is not None
                and caller.f_code is entry["application_code"]
                and type(journal) is cls
                and journal.ledger is entry["ledger"]
                and journal.ledger.caller is entry["caller"]
            ):
                if entry["journal"] is not None:
                    entry["poisoned"] = True
                else:
                    entry["journal"] = journal

        cls.__init__ = construct

    def install_ports(port_cls, result_cls):
        definition("review_sensei_github.operation_comments")
        original_init = port_cls.__init__
        original_create, original_update = port_cls.create, port_cls.update
        original_list = port_cls.list_comments
        original_request = port_cls._request
        original_read = result_cls.read_available_with_carriers
        original_functions.extend(
            (function, function.__code__)
            for function in (
                original_init,
                original_create,
                original_update,
                original_list,
                original_request,
                original_read,
            )
        )

        @wraps(original_init)
        def construct(port, *args, **kwargs):
            original_init(port, *args, **kwargs)
            if type(port) is port_cls:
                port_instances.add(port)

        def entry_for(port):
            entry = active.get() or analyzing.get()
            if entry is None or transport(port._http) is not entry["transport"]:
                return None
            return entry

        def denied(port):
            entry = entry_for(port)
            if entry is not None and entry["closed"]:
                ports_intact()
                raise ReviewInputError(
                    "materialized publication forbids new operation artifacts"
                )
            return entry

        @wraps(original_request)
        def request(port, method, path, **kwargs):
            if method not in {"GET", "HEAD"}:
                denied(port)
            return original_request(port, method, path, **kwargs)

        @wraps(original_create)
        def create(port, body):
            entry = denied(port)
            try:
                result = original_create(port, body)
                if entry is not None and class_intact(port):
                    entry["bodies"][result.comment_id] = (
                        result.body,
                        result.user_id,
                        result.user_type,
                    )
                return result
            except BaseException:
                if entry is not None:
                    entry["poisoned"] = True
                raise

        @wraps(original_update)
        def update(port, comment_id, body):
            entry = denied(port)
            try:
                result = original_update(port, comment_id, body)
                if entry is not None and class_intact(port):
                    entry["bodies"][result.comment_id] = (
                        result.body,
                        result.user_id,
                        result.user_type,
                    )
                return result
            except BaseException:
                if entry is not None:
                    entry["poisoned"] = True
                raise

        @wraps(original_list)
        def listed(port):
            result = original_list(port)
            entry = entry_for(port)
            if entry is not None and class_intact(port):
                for item in result:
                    entry["bodies"][item.comment_id] = (
                        item.body,
                        item.user_id,
                        item.user_type,
                    )
            return result

        @wraps(original_read)
        def read(store, *, event_key):
            result = original_read(store, event_key=event_key)
            entry = active.get() or analyzing.get()
            if entry is not None and result is not None and class_intact(store):
                digest, payload, ids = result
                if all(identifier in entry["bodies"] for identifier in ids):
                    entry["reads"][event_key] = (digest, bytes(payload), tuple(ids))
            return result

        port_cls.create, port_cls.update, port_cls.list_comments = (
            create,
            update,
            listed,
        )
        port_cls.__init__ = construct
        port_cls._request = request
        result_cls.read_available_with_carriers = read
        source_classes[port_cls] = class_seal(port_cls)
        source_classes[result_cls] = class_seal(result_cls)

    def install_retention(namespace):
        definition("review_sensei_github.publication_recovery")
        original = namespace["retain_review_result"]

        @wraps(original)
        def retain(**kwargs):
            result = original(**kwargs)
            entry = active.get()
            if entry is not None and kwargs["ledger"] is entry["ledger"]:
                value = kwargs["result"]
                if value.transaction is not None and not value.comments:
                    entry["retained"] = (
                        value.content_digest(),
                        canonical_bytes(value.transaction.to_dict()),
                    )
            return result

        namespace["retain_review_result"] = retain

    def require_closed_source(entry, ledger, identity):
        import hashlib
        import json

        from review_sensei.models import ReviewResult, ReviewTransaction

        from .accepted_result import selected_accepted_result
        from .guarded_pending_publication import _empty_state, _original_methods
        from .quarantine_origin import require_authenticated_read_owner

        ports_intact()
        app, caller = entry["app"], entry["caller"]
        journal = entry["journal"]
        if journal is None or entry["retained"] is None or entry["poisoned"]:
            return False
        for owner in (
            app,
            ledger,
            caller,
            journal,
            journal.reviewer,
            journal.reviewer.checks,
            journal.reviewer.finalizer,
        ):
            _original_methods(owner)
        record = caller._effect_record
        if (
            journal.ledger is not ledger
            or app.session_ledger is not ledger
            or record is not journal._record
            or record is None
            or caller._root != record.record_sha256
            or caller._checkpoint is not None
            or caller.accounting.uncertain
            or record.transaction is None
            or record.transaction.phase != "publication_failed"
        ):
            return False
        caller._require_ledger(ledger, identity)
        source = selected_accepted_result(
            ledger.http, record.transaction, app_id=ledger.authenticated_app_id
        )
        if (
            source is not entry["accepted"][1]
            or canonical_bytes(source.reference) != entry["accepted"][2]
        ):
            raise ReviewInputError("materialized accepted source changed")
        reference = source.reference
        original = ReviewResult.from_dict(json.loads(source.payload))
        retained_digest, retained_transaction = entry["retained"]
        if (
            original.comments
            or original.content_digest() != record.transaction.result_sha256
            or retained_digest != record.transaction.result_sha256
            or not ReviewTransaction.from_dict(
                json.loads(retained_transaction)
            ).logical_identity_matches(record.transaction)
            or reference["run_id"] != str(caller.run.run_id)
            or reference["run_attempt"] != str(caller.attempt)
        ):
            return False
        _empty_state(journal)
        reader = ledger._quarantine_origin_reader
        _original_methods(reader)
        _original_methods(reader.current_scope)
        authenticated = ledger._quarantine_authenticated
        require_authenticated_read_owner(authenticated, ledger, caller, identity)
        if canonical_bytes(record.quarantine_lineage) != authenticated.lineage_bytes:
            raise ReviewInputError("materialized origin lineage changed")
        if authenticated.record_sha256 != record.record_sha256:
            from .quarantine_checkpoint import confirmed_checkpoint_capacity

            if (
                confirmed_checkpoint_capacity(caller, ledger, identity).record
                is not record
            ):
                raise ReviewInputError("materialized current root custody changed")
        if (
            authenticated.archive["target_isolation"].get("schema_version")
            != "quarantine-target-isolation-v1"
        ):
            return False
        finalizer = journal.reviewer.finalizer
        if (
            any(
                getattr(finalizer, name) is not None
                for name in (
                    "partitioned_reader",
                    "human_inventory_reader",
                    "historical_assessment_reader_factory",
                    "native_union_reader_factory",
                    "prior_publication_reader_factory",
                )
            )
            or journal.reviewer._pending_thread_cleanup is not None
        ):
            return False
        from .application import GitHubApplication

        factory = finalizer.quarantine_reader_factory
        if (
            getattr(factory, "__self__", None) is not app
            or getattr(factory, "__func__", None)
            is not GitHubApplication._quarantine_hold_reader
        ):
            return False
        rows = tuple(ledger._cached_comments())
        by_id = {row["id"]: row for row in rows}
        ids = (reference["event_comment_id"], *reference["part_ids"])
        if len(by_id) != len(rows) or any(
            identifier not in by_id for identifier in ids
        ):
            return False
        event = by_id[reference["event_comment_id"]]
        if (
            hashlib.sha256(event["body"].encode()).hexdigest()
            != reference["event_body_sha256"]
        ):
            return False
        captured_bodies = dict(entry["accepted"][3])
        for identifier in ids:
            row = by_id[identifier]
            ledger._part_owner(row, identity)
            expected_body = captured_bodies.get(identifier)
            if expected_body is None or expected_body != (
                row["body"],
                row["user"]["id"],
                row["user"]["type"],
            ):
                raise ReviewInputError("materialized accepted carrier changed")
        if reference["part_ids"]:
            read = dict(entry["accepted"][4]).get(reference["event_key"])
            if read != (
                reference["payload_sha256"],
                source.payload,
                tuple(reference["part_ids"]),
            ):
                raise ReviewInputError("materialized accepted part receipt changed")
        from review_sensei.source_registry import require_module_seal

        from . import accepted_result, operation_comments, operation_host

        for module in (accepted_result, operation_host, operation_comments):
            require_module_seal(module)
        # This is a consistency calculation over the exact original observations,
        # not a fresh read or an alternative authority source. The actual census
        # continues fetching every complete native body independently.
        snapshot = accepted_result._ReadSnapshot(
            tuple(operation_comments._view(row) for row in rows)
        )
        store = operation_host.GitHubCommentOperationStore(
            snapshot, app_user_id=reference["producer_id"]
        )
        payload = accepted_result.read_accepted_result(
            reference,
            record.transaction,
            store,
            repository_id=reference["repository_id"],
            app_id=ledger.authenticated_app_id,
        )
        if payload != source.payload:
            raise ReviewInputError("materialized accepted payload changed")
        return True

    def require_journal_write(entry, request, path, payload):
        """Bind a physical write to one executing original journal request.

        This is a negative reachability fence, not a smaller census issuer.
        Retaining the frame until phase exit prevents frame-id reuse and denies
        an uncertain transport retry within the same source request.
        """
        from review_sensei import session
        from review_sensei.source_registry import require_module_seal

        from .guarded_pending_publication import _original_methods
        from .session_ledger import GitHubIssueCommentSessionLedger

        require_module_seal(session)
        ledger, journal = entry["ledger"], entry["journal"]
        if journal is None:
            raise ReviewInputError("materialized publication has no original journal")
        _original_methods(ledger)
        _original_methods(journal)
        from .quarantine_checkpoint import _CheckpointSnapshot

        caller = entry["caller"]
        snapshot = getattr(ledger, "_quarantine_checkpoint_snapshot", None)
        record = caller._effect_record
        if (
            journal.ledger is not ledger
            or entry["app"].session_ledger is not ledger
            or ledger.caller is not caller
            or journal._record is not record
            or record is None
            or record.record_sha256 != caller._root
            or type(snapshot) is not _CheckpointSnapshot
            or snapshot.ledger is not ledger
            or snapshot.caller is not caller
            or snapshot.identity != journal.identity
            or canonical_bytes(snapshot.record.to_dict())
            != canonical_bytes(record.to_dict())
        ):
            raise ReviewInputError("materialized publication physical root changed")
        _original_methods(snapshot)
        snapshot.require()
        if request.get_method() == "PATCH" and (
            path
            != ledger.http.repository_path(
                journal.identity.repository, f"/issues/comments/{snapshot.comment_id}"
            )
            or payload != {"body": snapshot.candidate_body}
        ):
            raise ReviewInputError(
                "materialized publication physical root candidate changed"
            )
        ledger_code = GitHubIssueCommentSessionLedger._request.__code__
        frame = inspect.currentframe()
        source = None
        journal_frame = False
        completion_frame = False
        while frame is not None:
            if frame.f_code is ledger_code and frame.f_locals.get("self") is ledger:
                if source is not None:
                    raise ReviewInputError("materialized publication writer is nested")
                source = frame
            if (
                frame.f_code is journal_checkpoint_sources.get(type(journal))
                and frame.f_locals.get("self") is journal
            ):
                journal_frame = True
            if (
                frame.f_code is session.complete_review_publication.__code__
                and frame.f_locals.get("ledger") is ledger
                and frame.f_back is not None
                and frame.f_back.f_code is entry["application_code"]
                and frame.f_back.f_locals.get("self") is entry["app"]
                and request.get_method() == "PATCH"
            ):
                completion_frame = True
            frame = frame.f_back
        if (
            source is None
            or not (journal_frame or completion_frame)
            or source in entry["write_frames"]
            or source.f_locals.get("method") != request.get_method()
            or source.f_locals.get("path") != path
            or canonical_bytes(source.f_locals.get("body")) != canonical_bytes(payload)
        ):
            raise ReviewInputError(
                "materialized publication forbids writes outside original journal request"
            )
        entry["write_frames"].append(source)

    def install_caller(cls):
        definition("review_sensei_github.api_run_requests")
        original = cls.open
        original_functions.append((original, original.__code__))

        @wraps(original)
        def opened(caller, request, *, timeout):
            entry = active.get()
            if entry is not None and entry["closed"] and entry["caller"] is caller:
                import json
                from urllib.parse import urlsplit
                from urllib.request import Request

                from .guarded_pending_publication import _original_methods
                from .operation_host import (
                    OPERATION_COMMENT_MARKER,
                    RESULT_COMMENT_MARKER,
                )

                _original_methods(caller)
                ports_intact()
                if type(request) is not Request:
                    raise ReviewInputError(
                        "materialized publication request source changed"
                    )
                if request.get_method() not in {"GET", "HEAD"}:
                    path = urlsplit(request.full_url).path
                    if "/issues/" in path and "/comments" in path:
                        data = request.data
                        if type(data) is not bytes or len(data) > 524288:
                            raise ReviewInputError(
                                "materialized publication request body changed"
                            )
                        payload = json.loads(data)
                        body = payload.get("body") if type(payload) is dict else None
                        if not isinstance(body, str) or any(
                            marker in body
                            for marker in (
                                OPERATION_COMMENT_MARKER,
                                RESULT_COMMENT_MARKER,
                            )
                        ):
                            raise ReviewInputError(
                                "materialized publication forbids new operation artifacts"
                            )
                        require_journal_write(entry, request, path, payload)
            return original(caller, request, timeout=timeout)

        cls.open = opened

    def install_publisher(cls):
        definition("review_sensei_github.publication")
        original = cls._publish_impl
        if hasattr(original, "__wrapped__"):
            raise ReviewInputError("materialized publisher is not defining source")
        publication_sources[cls] = original.__code__
        original_functions.append((original, original.__code__))

    def capture_projection(entry):
        from review_sensei.source_registry import require_module_seal

        from . import materialized_publication_projection as projection

        require_module_seal(projection)
        journal = entry["journal"]
        publisher = journal.reviewer
        code = publication_sources.get(type(publisher))
        frame = inspect.currentframe()
        found = None
        while frame is not None:
            if frame.f_code is code and frame.f_locals.get("self") is publisher:
                if found is not None:
                    raise ReviewInputError("materialized projection frame is nested")
                found = frame
            frame = frame.f_back
        if found is None:
            raise ReviewInputError("materialized projection executing source is absent")
        entry["projection"] = projection.project(journal, found.f_locals)

    def install_census(namespace):
        definition("review_sensei_github.quarantine_census")
        original = namespace["lifecycle_capacity_plan"]

        @wraps(original)
        def census(ledger, identity, *, added_parts):
            entry = active.get()
            if entry is not None and entry["caller"] is getattr(ledger, "caller", None):
                # Still no refined quote. Record source reachability only until
                # all materialization/source checks and writer coverage qualify.
                entry["censuses"] += 1
                qualified = require_closed_source(entry, ledger, identity)
                if entry["closed"] and not qualified:
                    raise ReviewInputError("materialized publication phase changed")
                if qualified:
                    entry["closed"] = True
                    if entry["censuses"] == 1:
                        try:
                            capture_projection(entry)
                        except ReviewInputError as error:
                            # A diagnostic miss cannot become an empty capacity
                            # scope or change the unchanged full runtime quote.
                            entry["projection_error"] = str(error)
            return original(ledger, identity, added_parts=added_parts)

        namespace["lifecycle_capacity_plan"] = census

    def observation(ledger):
        entry = active.get()
        if entry is None or entry["caller"] is not getattr(ledger, "caller", None):
            return None
        return {
            "retained": entry["retained"] is not None,
            "confirmed_read_events": len(entry["reads"]),
            "observed_bodies": len(entry["bodies"]),
            "poisoned": entry["poisoned"],
            "closed": entry["closed"],
            "censuses": entry["censuses"],
            "projection": entry["projection"],
            "projection_error": entry["projection_error"],
        }

    return (
        install_analysis,
        install_application,
        install_ports,
        install_journal,
        install_retention,
        install_caller,
        install_census,
        install_publisher,
        observation,
    )


(
    install_analysis,
    install_application,
    install_ports,
    install_journal,
    install_retention,
    install_caller,
    install_census,
    install_publisher,
    phase_observation,
) = _build()
del _build
