"""Source-issued finite cleanup order. Sizing never grants mutation authority."""

from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

_ISSUED: WeakKeyDictionary = WeakKeyDictionary()


@dataclass(eq=False)
class OrderedNativeBatch:
    journal: Any
    record: Any
    root: bytes
    operation_id: str
    head_sha: str
    requests: tuple[bytes, ...]
    confirmed_prefix: int = field(default=0, init=False)
    _approved: bool = field(default=False, init=False)
    _bound: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False)
    _inflight: bool = field(default=False, init=False)

    def _signature(self):
        return (
            id(self.journal),
            id(self.record),
            self.root,
            self.operation_id,
            self.head_sha,
            self.requests,
            self.confirmed_prefix,
            self._approved,
            self._bound,
            self._closed,
            self._inflight,
        )

    def _intact(self):
        return _ISSUED.get(self) == self._signature()

    def _commit(self):
        _ISSUED[self] = self._signature()

    def approve(self):
        if not self._intact() or self._bound or self._closed:
            raise ReviewInputError("ordered native batch preflight is unavailable")
        self._approved = True
        self._commit()
        return self

    def bind(self, requests):
        if (
            not self._intact()
            or not self._approved
            or self._bound
            or self._closed
            or self.record is not self.journal._record
            or self.root != canonical_bytes(self.journal._record.to_dict())
            or self.operation_id != self.journal.operation_id
            or self.head_sha != self.journal.head_sha
            or self.requests != tuple(canonical_bytes(request) for request in requests)
        ):
            raise ReviewInputError("ordered native batch source changed")
        self._bound = True
        self._commit()
        return self

    def before_request(self, method, path, body):
        if not self._intact():
            raise ReviewInputError("ordered native batch state changed")
        encoded = canonical_bytes((method, path, body))
        if encoded not in self.requests:
            if (
                method == "POST"
                and path == "/graphql"
                and isinstance(body, dict)
                and (
                    body.get("operationName") == "ResolveReviewThread"
                    or "resolveReviewThread" in str(body.get("query", ""))
                )
            ):
                raise ReviewInputError("ordered native batch request contents changed")
            return False
        if (
            not self._intact()
            or not self._bound
            or self._closed
            or self._inflight
            or self.confirmed_prefix >= len(self.requests)
            or encoded != self.requests[self.confirmed_prefix]
            or self.operation_id != self.journal.operation_id
            or self.head_sha != self.journal.head_sha
        ):
            raise ReviewInputError(
                "ordered native batch dispatch is outside its prefix"
            )
        self._inflight = True
        self._commit()
        return True

    def confirm_current(self):
        if not self._intact() or not self._bound or self._closed or not self._inflight:
            raise ReviewInputError("ordered native batch confirmation is unavailable")
        self.confirmed_prefix += 1
        self._inflight = False
        self._commit()

    def stop(self):
        if not self._intact():
            raise ReviewInputError("ordered native batch state changed")
        if self.confirmed_prefix < len(self.requests):
            from .command_finalization_plan import stop_command_cleanup

            stop_command_cleanup(self.journal)
        self._closed = True
        self._commit()

    def close(self):
        self.stop()


def issue_ordered_batch(journal, requests):
    """Issue unopened sizing scope; approve only after the complete preflight."""
    encoded = tuple(canonical_bytes(request) for request in requests)
    if len(encoded) < 2 or len(set(encoded)) != len(encoded):
        raise ReviewInputError("ordered native batch scope is invalid")
    witness = OrderedNativeBatch(
        journal,
        journal._record,
        canonical_bytes(journal._record.to_dict()),
        journal.operation_id,
        journal.head_sha,
        encoded,
    )
    witness._commit()
    return witness


def bind_ordered_batch(witness, requests):
    if type(witness) is not OrderedNativeBatch or not witness._intact():
        return None
    return witness.bind(requests)
