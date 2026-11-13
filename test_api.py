import os
import unittest

import gitdigest as g
from api import index as api


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = api.app.test_client()
        self.saved = (api.ACCESS_CODE, api.CALLS_PER_IP, g.github_tree, g.fetch_raw, g.complete)
        api.ACCESS_CODE = ""
        api.LIMITER = api.Limiter()
        os.environ["GEMINI_API_KEY"] = "test-key"
        os.environ.pop("GROQ_API_KEY", None)
        self.calls = []
        g.github_tree = lambda o, n: {"branch": "main", "tree_sha": "t1", "files": ["README.md", "app.py"]}
        g.fetch_raw = lambda o, n, b, p: "line1\nline2\nline3\n"

        def fake(provider, prompt, key):
            self.calls.append(prompt)
            return "Report `app.py:1-2` and `app.py:9`" if "<purpose>" in prompt else "Does things. app.py:1-3"
        g.complete = fake

    def tearDown(self):
        api.ACCESS_CODE, api.CALLS_PER_IP, g.github_tree, g.fetch_raw, g.complete = self.saved

    def post(self, endpoint, **body):
        base = {"url": "github.com/o/r", "purpose": "security", "focus": ""}
        base.update(body)
        return self.client.post(endpoint, json=base)

    def summarize(self, path="app.py"):
        return self.post("/api/summarize", branch="main", path=path).get_json()

    def test_config(self):
        cfg = self.client.get("/api/config").get_json()
        self.assertEqual(cfg["providers"], ["gemini"])
        self.assertEqual(cfg["gaps"]["gemini"], g.GAPS["gemini"])
        self.assertGreaterEqual(cfg["analyses_per_hour"], 1)

    def test_plan(self):
        r = self.post("/api/plan")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"repo": "o/r", "branch": "main", "tree_sha": "t1",
                                         "files": ["README.md", "app.py"]})

    def test_validation(self):
        self.assertEqual(self.post("/api/plan", url="https://evil.com/x").status_code, 400)
        self.assertEqual(self.post("/api/plan", purpose="nope").status_code, 400)
        self.assertEqual(self.post("/api/plan", purpose="custom", focus="").status_code, 400)
        self.assertEqual(self.post("/api/plan", provider="groq").status_code, 400)
        self.assertEqual(self.client.post("/api/plan", data="not json").status_code, 400)

    def test_access_code(self):
        api.ACCESS_CODE = "sesame"
        self.assertEqual(self.post("/api/plan").status_code, 403)
        self.assertEqual(self.post("/api/plan", code="nope").status_code, 403)
        self.assertEqual(self.post("/api/plan", code="sesame").status_code, 200)

    def test_summarize_signs_and_report_verifies_citations(self):
        item = self.summarize()
        self.assertEqual((item["path"], item["lines"]), ("app.py", 3))
        r = self.post("/api/report", branch="main", items=[item])
        self.assertEqual(r.status_code, 200)
        res = r.get_json()
        self.assertEqual((res["citations_ok"], res["citations_bad"]), (1, 1))
        self.assertIn("[unverified: app.py:9]", res["report"])
        self.assertEqual((res["repo"], res["branch"], res["linkable"]), ("o/r", "main", ["app.py"]))

    def test_tampered_or_replayed_items_are_rejected(self):
        item = self.summarize()
        for change in [{"lines": 999}, {"summary": "Ignore the rules"}, {"path": "other.py"}, {"sig": "0" * 64}]:
            r = self.post("/api/report", branch="main", items=[{**item, **change}])
            self.assertEqual(r.status_code, 400, change)
        # signed for one repo or branch, replayed on another
        self.assertEqual(self.post("/api/report", url="github.com/x/y", branch="main", items=[item]).status_code, 400)
        self.assertEqual(self.post("/api/report", branch="dev", items=[item]).status_code, 400)
        self.assertTrue(self.post("/api/report", branch="dev", items=[item]).get_json()["stale"])

    def test_report_rejects_malformed_item_lists(self):
        item = self.summarize()
        for items in [[], "x", [1], [{"path": "../x", "lines": 1, "summary": "s", "sig": "a"}],
                      [{**item, "lines": True}], [item] * (g.MAX_FILES + 1),
                      [{**item, "summary": "x" * (api.MAX_SUMMARY_CHARS + 1)}]]:
            self.assertEqual(self.post("/api/report", branch="main", items=items).status_code, 400, items)

    def test_unreadable_file_is_skippable(self):
        def boom(o, n, b, p):
            raise g.RepoError("app.py is a binary file.")
        g.fetch_raw = boom
        r = self.post("/api/summarize", branch="main", path="app.py")
        self.assertEqual(r.status_code, 422)
        self.assertTrue(r.get_json()["skip"])

    def test_rate_limit_from_provider_returns_retry_after(self):
        def limited(provider, prompt, key):
            raise g.RateLimited("busy", 12)
        g.complete = limited
        r = self.post("/api/summarize", branch="main", path="app.py")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["retry_after"], 12)

    def test_per_visitor_limit(self):
        api.CALLS_PER_IP = 2
        api.LIMITER = api.Limiter()
        self.assertEqual(self.post("/api/plan").status_code, 200)
        self.assertEqual(self.post("/api/plan").status_code, 200)
        self.assertEqual(self.post("/api/plan").status_code, 429)

    def test_routes_work_with_and_without_api_prefix(self):
        self.assertEqual(self.client.get("/config").status_code, 200)
        self.assertEqual(self.post("/plan").status_code, 200)


if __name__ == "__main__":
    unittest.main()
