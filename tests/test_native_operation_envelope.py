"""Encoded metadata maxima use the real closed event/index producers."""

import hashlib
import unittest

from review_sensei_github.native_operation_envelope import (
    OPERATION_WIRE_BOUND,
    operation_envelope_certificate,
    require_operation_envelope,
)
from review_sensei_github.operation_host import (
    _EVENT_REQUIRED,
    _TRANSITION_FIELDS,
    _decode_event,
    comment_payload,
    encode_event_comment,
    encode_index_comment,
)
from review_sensei_github.packed_comment_census import wire

from review_sensei.errors import ReviewInputError


def pending_metadata(transitions=0, grants=0):
    event = {key: 0 for key in _EVENT_REQUIRED}
    for key in ("binding", "event_id", "operation_id", "root_sha", "scope_digest"):
        event[key] = "a" * 64
    for key in (
        "expired_observed",
        "inference_blocked",
        "provider_charged",
        "provider_unknown",
        "quarantine",
        "ack_attempted",
    ):
        event[key] = False
    event["accepted"] = None
    key = ("a" * 64, "a" * 64)
    pending = {"events": {key: event}, "transitions": {}, "grants": {}, "scopes": {}}
    for ordinal in range(transitions):
        raw = {name: 0 for name in _TRANSITION_FIELDS}
        for name in (
            "scope_digest",
            "event_id",
            "attempt_id",
            "prior_root_sha",
            "target_root_sha",
            "request_digest",
            "dispatch_digest",
        ):
            raw[name] = "a" * 64
        attempt = hashlib.sha256(str(ordinal).encode()).hexdigest()
        raw.update(
            attempt_id=attempt,
            encoded="a",
            state="pending",
            obligation_ids=("a", "b", "c", "d"),
        )
        pending["transitions"][(*key, attempt)] = raw
    for ordinal in range(grants):
        pending["grants"][hashlib.sha256(str(ordinal).encode()).hexdigest()] = True
    pending["scopes"][key[0]] = {"root_sha": "a" * 64, "root_generation": 0}
    return key, pending


class NativeOperationEnvelopeTests(unittest.TestCase):
    def test_full_sixty_kib_event_variants_fit_the_encoded_envelope(self):
        certificate = operation_envelope_certificate()
        self.assertLessEqual(certificate["encoded_upper_bound"], OPERATION_WIRE_BOUND)
        for transitions, grants in ((0, 0), (0, 700), (1, 200), (64, 32)):
            key, pending = pending_metadata(transitions, grants)
            low, high = 0, 45000
            while low < high:
                middle = (low + high + 1) // 2
                try:
                    encode_event_comment(key, pending, result=b"x" * middle)
                except ValueError:
                    high = middle - 1
                else:
                    low = middle
            body = encode_event_comment(key, pending, result=b"x" * low)
            with self.subTest(transitions=transitions, grants=grants):
                self.assertGreaterEqual(len(body.encode()), 59996)
                self.assertLessEqual(wire({"body": body}), OPERATION_WIRE_BOUND)
                require_operation_envelope(body)

    def test_index_digest_entries_preserve_the_same_encoded_bound(self):
        _, pending = pending_metadata(0, 500)
        pending["events"] = {
            ("a" * 64, hashlib.sha256(str(number).encode()).hexdigest()): {}
            for number in range(100)
        }
        body = encode_index_comment(pending)
        self.assertLessEqual(wire({"body": body}), OPERATION_WIRE_BOUND)
        require_operation_envelope(body)

    def test_legacy_short_grant_keys_remain_readable_but_are_not_this_producer_profile(
        self,
    ):
        import json

        key, pending = pending_metadata()
        pending["grants"]["legacy"] = True
        body = encode_event_comment(key, pending)
        _decode_event(json.loads(comment_payload(body)))
        with self.assertRaises(ReviewInputError):
            require_operation_envelope(body)
