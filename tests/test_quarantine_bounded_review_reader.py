"""Fixed-width cold walks preserve lifetime allocations and physical caps."""

import sys
import unittest
from urllib.parse import parse_qs, urlsplit

from fake_github_http import FakeHTTPResponse, json_response
from review_sensei_github.errors import GitHubHTTPError
from review_sensei_github.http import MAX_GITHUB_RESPONSE_BYTES, GitHubHttp
from review_sensei_github.quarantine_scan_capacity import (
    PAGE_BYTES,
    bounded_review_page,
    capacity_review_walk,
)

from review_sensei.errors import ReviewInputError


class BoundedReviewReaderTests(unittest.TestCase):
    def test_fixed_width_prices_final_short_or_empty_page(self):
        for count in (0, 1, 7, 14, 15, 99, 100, 1000):
            for largest in (0, 1, 8192, 70656, PAGE_BYTES):
                with self.subTest(count=count, largest=largest):
                    width, calls = bounded_review_page(count, largest)
                    self.assertLessEqual(width * largest, PAGE_BYTES)
                    self.assertLessEqual(width, 100)
                    self.assertEqual(calls, count // width + 1)
        self.assertEqual(bounded_review_page(15, 70656), (7, 3))
        self.assertEqual(bounded_review_page(14, 70656), (7, 3))
        for count, largest in (
            (True, 1),
            (-1, 1),
            (1, True),
            (1, -1),
            (1, PAGE_BYTES + 1),
        ):
            with self.subTest(count=count, largest=largest):
                with self.assertRaises(ReviewInputError):
                    bounded_review_page(count, largest)

    def test_fixed_width_transport_charges_every_page_and_never_retries_loss(self):
        rows = [{"id": n + 1, "body": "x" * 65000} for n in range(15)]
        for failure in (None, "loss", "oversize", "rejected"):
            with self.subTest(failure=failure):
                calls = []

                def opener(request, timeout):
                    query = parse_qs(urlsplit(request.full_url).query)
                    width, page = int(query["per_page"][0]), int(query["page"][0])
                    calls.append((width, page))
                    if page == 2 and failure == "loss":
                        raise TimeoutError("synthetic response loss")
                    if page == 2 and failure == "oversize":
                        return FakeHTTPResponse(b" " * (MAX_GITHUB_RESPONSE_BYTES + 1))
                    if page == 2 and failure == "rejected":
                        return json_response({"message": "refused"}, 403)
                    return json_response(rows[(page - 1) * width : page * width])

                http = GitHubHttp(api_url="https://api.github.test", opener=opener)
                arguments = dict(
                    path="/repos/owner/repo/pulls/7/reviews",
                    token="synthetic",
                    page_sizes=(7,),
                    max_requests=3,
                )
                if failure is None:
                    self.assertEqual(http.paginate(**arguments), rows)
                    self.assertEqual(calls, [(7, 1), (7, 2), (7, 3)])
                else:
                    with self.assertRaises(GitHubHTTPError):
                        http.paginate(**arguments)
                    self.assertEqual(calls, [(7, 1), (7, 2)])

    def test_actual_installed_guard_uses_fresh_bounded_walk_without_admission(self):
        from test_quarantine_finalizer_observation import (
            QuarantineFinalizerObservationTests,
        )

        observed = []
        code = capacity_review_walk.__code__

        def observe(frame, event, result):
            if frame.f_code is code and event == "return" and result is not None:
                observed.append(result)
                for rows, text in (
                    ([{"id": 1}, {"id": 1}], "identity"),
                    ([{"id": True}], "identity"),
                    (
                        [
                            {"id": n + 1}
                            for n in range(result.certificate["count_upper_bound"] + 1)
                        ],
                        "item bound",
                    ),
                    (
                        [
                            {
                                "id": 1,
                                "body": "x"
                                * (result.certificate["largest_upper_bound"] + 1),
                            }
                        ],
                        "item wire bound",
                    ),
                ):
                    with self.assertRaisesRegex(ReviewInputError, text):
                        result.validate(rows)
            return observe if frame.f_code is code else None

        prior = sys.gettrace()
        try:
            sys.settrace(observe)
            case = QuarantineFinalizerObservationTests()
            case.exercise(published_count=1, approval=False, measure_inline=True)
        finally:
            sys.settrace(prior)
        self.assertTrue(observed)
        self.assertEqual(case.compiler_receipt["frozen_limit"], 512)
        self.assertEqual(case.compiler_receipt["remaining_unallocated"], 190)
        self.assertEqual(case.compiler_receipt["blocks"][-1]["undispatched"], 50)
        certificate = case.compiler_receipt["guard_certificates"]["reviews"]
        self.assertEqual(certificate["reader"], "bounded-cold-reviews")
        self.assertEqual(certificate["physical_reads_upper_bound"], 3)
        self.assertEqual(certificate["count_upper_bound"], 15)
        for plan in observed:
            self.assertLessEqual(plan.requests, 10)
            self.assertLessEqual(
                plan.width * plan.certificate["largest_upper_bound"], PAGE_BYTES
            )
