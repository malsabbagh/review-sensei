"""Source-issued nonuniform page recipes retain the entire bounded frontier."""

import copy
import json
import tempfile
import unittest
from urllib.parse import urlsplit

import test_p1_public_profile as p1
from fake_github_http import json_response
from review_sensei_github.api_run_requests import _PLAIN_FINDING_ACTIVE


class PlainFindingPageRecipeTests(unittest.TestCase):
    def test_actual_issued_recipe_refuses_changed_native_frontiers(self):
        for fault in ("changed", "grown", "reordered", "oversized"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = p1.P1PublicProfileTests()._entry(
                    directory,
                    [
                        (3000 + i, "m" * 60_000 if i < 10 else "note", i < 10)
                        for i in range(24)
                    ],
                )
                external = fixture.external
                injected = []

                def response(request, timeout):
                    result = external(request, timeout)
                    active = [
                        (ledger, plan)
                        for ledger, (plan, consumed) in list(
                            _PLAIN_FINDING_ACTIVE.items()
                        )
                        if consumed and ledger.caller is fixture.accounted
                    ]
                    if (
                        not injected
                        and active
                        and request.method == "GET"
                        and urlsplit(request.full_url).path.endswith(
                            "/issues/7/comments"
                        )
                    ):
                        _, plan = active[0]
                        self.assertEqual(
                            plan.page_requests[:3], ((5, 1), (5, 2), (10, 2))
                        )
                        self.assertLessEqual(len(plan.page_requests), 10)
                        self.assertTrue(all(b < 512 * 1024 for b in plan.page_bounds))
                        rows = json.loads(result.body)
                        if fault == "changed":
                            rows[0]["id"] = 999999
                        elif fault == "grown":
                            rows.append(copy.deepcopy(rows[0]))
                        elif fault == "reordered":
                            rows[0], rows[1] = rows[1], rows[0]
                        else:
                            rows[0]["body"] = "z" * (512 * 1024)
                        injected.append(request.full_url)
                        return json_response(rows)
                    return result

                fixture.external = response
                self.assertEqual(fixture.invoke(), 1, fixture.error)
                self.assertEqual(len(injected), 1)
                self.assertFalse(fixture.reviews)
                self.assertFalse(fixture.inline)
                self.assertFalse(
                    any(c.get("conclusion") == "success" for c in fixture.check_runs)
                )
                self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
