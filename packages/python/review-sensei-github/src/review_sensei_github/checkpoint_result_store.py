"""Reader-first checkpoint projections over the original bounded part store."""

from __future__ import annotations

import hashlib
import json
from weakref import WeakKeyDictionary

from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult

from .accepted_result import (
    _ReadSnapshot,
    read_accepted_result,
    selected_accepted_result,
)
from .operation_comments import ResultPartStore
from .retention_projection import (
    CHECKPOINT_VERSION,
    MAX_CHECKPOINT_PROJECTION_BYTES,
    NATIVE_RENDER_FORMAT,
    checkpoint_projection,
    restore_checkpoint_projection,
)

_READ_FORMATS: WeakKeyDictionary = WeakKeyDictionary()


def uses_literal_publication(http, result):
    """A successfully authenticated descriptor selects its exact native format.

    This cache retains no response bytes and grants no authority. Raw legacy
    results keep the legacy renderer, including an uncertain original POST.
    """
    transaction = result.transaction
    if http is None or transaction is None:
        return False
    from .publication_recovery import _publication_transport

    return (
        _READ_FORMATS.get(_publication_transport(http), {}).get(
            (transaction.transaction_id, transaction.result_sha256)
        )
        == NATIVE_RENDER_FORMAT
    )


class CheckpointResultStore(ResultPartStore):
    """Expose canonical result bytes; the physical descriptor stays distinct."""

    def __init__(
        self, port, *, app_user_id, http, identity, transaction, app_id, compact_write
    ):
        super().__init__(port, app_user_id=app_user_id)
        self._raw = ResultPartStore(port, app_user_id=app_user_id)
        self.http, self.identity, self.transaction = http, identity, transaction
        self.app_id, self.compact_write = app_id, compact_write
        self._last_read_was_projection = False

    @property
    def last_read_was_projection(self):
        return self._last_read_was_projection

    def physical_part_count(self, result):
        from .operation_comments import ensure_result_part_capacity
        from .publication_recovery import _publication_transport

        if (
            getattr(
                _publication_transport(self.http), "_quarantine_capacity_guard", None
            )
            is None
        ):
            return super().physical_part_count(result)
        if not self.compact_write or self.transaction is None:
            raise ReviewInputError("missing compact checkpoint cannot be recreated")
        # The source is authenticated before the write; this reserves its
        # closed descriptor maximum, never a fresh full-result copy.
        return ensure_result_part_capacity(MAX_CHECKPOINT_PROJECTION_BYTES)

    def _source(self, reference, store=None):
        if self.transaction is None:
            raise ReviewInputError(
                "compact checkpoint has no independently selected transaction"
            )
        return read_accepted_result(
            reference,
            self.transaction,
            self._raw if store is None else store,
            repository_id=self.identity.repository_id,
            app_id=self.app_id,
        )

    def _decode(self, payload, store=None):
        self._last_read_was_projection = False
        value = json.loads(payload)
        if (
            isinstance(value, dict)
            and value.get("schema_version") == CHECKPOINT_VERSION
        ):
            restored = restore_checkpoint_projection(
                payload, self.transaction, lambda ref: self._source(ref, store)
            )
            self._last_read_was_projection = True
            from .publication_recovery import _publication_transport

            selected = _READ_FORMATS.setdefault(_publication_transport(self.http), {})
            selected[
                self.transaction.transaction_id, self.transaction.result_sha256
            ] = NATIVE_RENDER_FORMAT
            return restored
        # Legacy canonical full results keep their original independent checks.
        return payload

    def read_available_with_carriers(self, *, event_key):
        snapshot = ResultPartStore(
            _ReadSnapshot(self._port.list_comments()), app_user_id=self._app_user_id
        )
        available = snapshot.read_available_with_carriers(event_key=event_key)
        if available is None:
            return None
        _storage_digest, payload, identifiers = available
        decoded = self._decode(payload, snapshot)
        return hashlib.sha256(decoded).hexdigest(), decoded, identifiers

    def read_available(self, *, event_key):
        available = self.read_available_with_carriers(event_key=event_key)
        return available[:2] if available is not None else None

    def original_reference(self, *, event_key):
        """Bind physical carriers as well as the reconstructed complete result."""
        from .publication_recovery import _event_key

        if self.transaction is None or event_key != _event_key(
            self.identity, self.transaction
        ):
            raise ReviewInputError(
                "checkpoint reference selected transaction conflicts"
            )
        snapshot = ResultPartStore(
            _ReadSnapshot(self._port.list_comments()), app_user_id=self._app_user_id
        )
        available = snapshot.read_available_with_carriers(event_key=event_key)
        if available is None:
            raise ReviewInputError("original checkpoint reference is missing")
        storage_digest, stored, identifiers = available
        payload = self._decode(stored, snapshot)
        return {
            "schema_version": "checkpoint-physical-reference-v1",
            "event_key": event_key,
            "storage_payload_sha256": storage_digest,
            "carrier_ids": list(identifiers),
            "result_payload_sha256": hashlib.sha256(payload).hexdigest(),
        }, payload

    def read(self, *, event_key, payload_digest):
        available = self.read_available_with_carriers(event_key=event_key)
        if available is None or available[0] != payload_digest:
            raise ReviewInputError("checkpoint complete result digest conflicts")
        return available[1]

    def write(self, *, event_key, result, _check_existing=True):
        from .publication_recovery import _publication_transport

        guard = getattr(
            _publication_transport(self.http), "_quarantine_capacity_guard", None
        )
        if guard is None:
            return self._raw.write(
                event_key=event_key, result=result, _check_existing=_check_existing
            )
        if not self.compact_write or self.transaction is None:
            raise ReviewInputError("missing compact checkpoint cannot be recreated")
        captured = selected_accepted_result(
            self.http, self.transaction, app_id=self.app_id
        )
        if captured.native_format != NATIVE_RENDER_FORMAT:
            raise ReviewInputError(
                "compact checkpoint cannot change the original producer format"
            )
        original = self._source(captured.reference)
        if original != captured.payload:
            raise ReviewInputError("original accepted result changed before checkpoint")
        bound = ReviewResult.from_dict(json.loads(result))
        descriptor = checkpoint_projection(original, bound, captured.reference)
        self._raw.write(
            event_key=event_key, result=descriptor, _check_existing=_check_existing
        )
        return hashlib.sha256(result).hexdigest()
