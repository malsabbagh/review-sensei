"""One complete receipt-page read is bounded independently of write admission."""

import copy
import unittest
from urllib.parse import parse_qs, urlsplit

from fake_github_http import FakeHTTPResponse, json_response
from review_sensei_github.errors import GitHubHTTPError
from review_sensei_github.http import MAX_GITHUB_RESPONSE_BYTES, GitHubHttp
from review_sensei_github.quarantine_command_receipt import (
    METADATA_BYTES,
    freeze_receipt_membership,
    project_receipt_page,
    read_receipt_page,
)

from review_sensei.errors import ReviewInputError


def row(identifier, body):
    return {"id": identifier, "body": body, "user": {"id": 999, "type": "Bot"}}


class ReceiptPageTests(unittest.TestCase):
    def prepare(self, *, update=False):
        rows = [row(1, "current root"), row(2, "unchanged historical body")]
        text = "new receipt <!-- marker -->"
        if update:
            rows.append(row(3, "previous receipt <!-- marker -->"))
        projection = project_receipt_page(
            rows,
            mutable_bounds=((1, 66560), (3, 65536)) if update else ((1, 66560),),
            future_bounds=(32768,) if update else (32768, 65536),
        )
        final = copy.deepcopy(rows)
        final[0]["body"] = "confirmed final root"
        final.append(row(4, "source-confirmed own event"))
        membership = freeze_receipt_membership(
            final,
            projection=projection,
            receipt_text=text,
            update_id=3 if update else None,
            app_id=999,
        )
        return final, membership

    def read(self, payload, membership, *, reconcile=False):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(query, {"per_page": ["100"], "page": ["1"]})
            if isinstance(payload, BaseException):
                raise payload
            if isinstance(payload, FakeHTTPResponse):
                return payload
            return json_response(payload)

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        try:
            return read_receipt_page(
                http,
                path="/repos/owner/repo/issues/7/comments",
                token="synthetic",
                membership=membership,
                reconcile=reconcile,
            )
        finally:
            self.assertEqual(len(calls), 1)

    def test_exact_known_rows_and_only_own_create_or_same_marker_update(self):
        for update in (False, True):
            with self.subTest(update=update):
                rows, membership = self.prepare(update=update)
                self.assertEqual(self.read(rows, membership), rows)
                # An absent lost write remains observable as absent, never retried.
                self.assertEqual(self.read(rows, membership, reconcile=True), rows)
                stored = copy.deepcopy(rows)
                if update:
                    next(r for r in stored if r["id"] == 3)["body"] = (
                        membership.receipt_text
                    )
                else:
                    stored.append(row(5, membership.receipt_text))
                self.assertEqual(self.read(stored, membership, reconcile=True), stored)
                with self.assertRaises(ReviewInputError):
                    self.read(stored, membership)

    def test_membership_body_identity_metadata_and_complete_wire_growth_refuse(self):
        rows, membership = self.prepare()
        bad = []
        bad.append(rows + [row(7, "late external allocation")])
        bad.append(rows[:-1])
        bad.append(rows + [copy.deepcopy(rows[0])])
        changed = copy.deepcopy(rows)
        changed[0]["user"]["id"] = 998
        bad.append(changed)
        changed = copy.deepcopy(rows)
        changed[0]["body"] += " edited"
        bad.append(changed)
        changed = copy.deepcopy(rows)
        changed[0]["id"] = True
        bad.append(changed)
        changed = copy.deepcopy(rows)
        changed[0]["extra"] = "x" * (METADATA_BYTES + 1)
        bad.append(changed)
        bad.append([row(n + 1, "") for n in range(100)])
        wrong_owner = rows + [row(5, membership.receipt_text)]
        wrong_owner[-1]["user"]["id"] = 998
        bad.append(wrong_owner)
        bad.append(
            rows + [row(5, membership.receipt_text), row(6, membership.receipt_text)]
        )
        for payload in bad:
            with self.subTest(payload=str(payload)[:80]):
                with self.assertRaises(ReviewInputError):
                    self.read(payload, membership, reconcile=True)
        for failure in (
            TimeoutError("response lost"),
            FakeHTTPResponse(b" " * (MAX_GITHUB_RESPONSE_BYTES + 1)),
            json_response({"message": "denied"}, 403),
        ):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises((ReviewInputError, GitHubHTTPError)):
                    self.read(failure, membership, reconcile=True)

    def test_projection_counts_escaped_bodies_metadata_and_every_future_allocation(
        self,
    ):
        small = [row(1, "\\" * 100)]
        projection = project_receipt_page(
            small, mutable_bounds=((1, 66560),), future_bounds=(32768, 65536)
        )
        self.assertEqual(
            projection.maximum_bytes,
            2 + 66560 + 32768 + 65536 + 3 * (METADATA_BYTES + 1),
        )
        for bodies in (
            [row(n + 1, "x") for n in range(99)],
            [row(n + 1, "\\" * 30000) for n in range(8)],
        ):
            with self.assertRaises(ReviewInputError):
                project_receipt_page(bodies, future_bounds=(65536,))
        rows, membership = self.prepare()
        with self.assertRaises(ReviewInputError):
            freeze_receipt_membership(
                rows + [row(88, "unfunded" * 50000)],
                projection=projection,
                receipt_text=membership.receipt_text,
                update_id=None,
                app_id=999,
            )
