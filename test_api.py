import os
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

import gitdigest as g
from api import index as api

SECRET = "s" * 32


class FakeJson:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


class Base(unittest.TestCase):
    def setUp(self):
        self.env = dict(os.environ)
        os.environ.update({
            "SESSION_SECRET": SECRET, "GEMINI_API_KEY": "test-key",
            "GITHUB_CLIENT_ID": "gh-id", "GITHUB_CLIENT_SECRET": "gh-secret",
            "GOOGLE_CLIENT_ID": "g-id", "GOOGLE_CLIENT_SECRET": "g-secret",
        })
        os.environ.pop("GROQ_API_KEY", None)
        self.saved = (api.ACCESS_CODE, api.CALLS_PER_USER, api.SECURE_COOKIES,
                      g.github_tree, g.fetch_raw, g.complete)
        api.ACCESS_CODE, api.SECURE_COOKIES = "", False
        api.LIMITER = api.Limiter()
        self.client = api.app.test_client()
        self.anon = api.app.test_client()
        self.client.set_cookie("gd_session", self.session())
        g.github_tree = lambda o, n: {"branch": "main", "tree_sha": "t1", "files": ["README.md", "app.py"]}
        g.fetch_raw = lambda o, n, b, p: "line1\nline2\nline3\n"
        g.complete = self.fake_complete

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        (api.ACCESS_CODE, api.CALLS_PER_USER, api.SECURE_COOKIES,
         g.github_tree, g.fetch_raw, g.complete) = self.saved

    @staticmethod
    def fake_complete(provider, prompt, key):
        if "<purpose>" in prompt:
            return "Report `app.py:1-2` and `app.py:9`"
        return "Does things. app.py:1-3"

    def session(self, **over):
        data = {"k": "session", "exp": time.time() + 3600, "p": "github", "id": "42",
                "name": "Collins", "login": "collinxweb", "avatar": ""}
        data.update(over)
        return api.sign_token(data)

    def post(self, endpoint, client=None, **body):
        base = {"url": "github.com/o/r", "purpose": "security", "focus": ""}
        base.update(body)
        return (client or self.client).post(endpoint, json=base)

    def summarize(self):
        return self.post("/api/summarize", branch="main", path="app.py").get_json()


class ConfigAndAuthTests(Base):
    def test_config_reports_login_state(self):
        cfg = self.anon.get("/api/config").get_json()
        self.assertEqual(cfg["providers"], ["gemini"])
        self.assertEqual(cfg["login"], {"github": True, "google": True})
        self.assertTrue(cfg["login_ready"])

    def test_me_is_null_without_a_session_and_user_with_one(self):
        self.assertIsNone(self.anon.get("/api/me").get_json()["user"])
        self.assertEqual(self.client.get("/api/me").get_json()["user"]["login"], "collinxweb")

    def test_protected_endpoints_need_sign_in(self):
        self.assertEqual(self.post("/api/plan", client=self.anon).status_code, 401)
        self.assertEqual(self.post("/api/summarize", client=self.anon, branch="main", path="a.py").status_code, 401)
        self.assertEqual(self.post("/api/report", client=self.anon, branch="main", items=[]).status_code, 401)

    def test_tampered_expired_and_wrong_kind_tokens_are_rejected(self):
        good = self.session()
        for bad in [good[:-2] + "xx", self.session(exp=time.time() - 1), self.session(k="state")]:
            c = api.app.test_client()
            c.set_cookie("gd_session", bad)
            self.assertEqual(self.post("/api/plan", client=c).status_code, 401)

    def test_login_refused_when_session_secret_missing(self):
        os.environ.pop("SESSION_SECRET")
        self.assertEqual(self.anon.get("/api/auth/github/start").status_code, 302)
        self.assertIn("github_unavailable", self.anon.get("/api/auth/github/start").headers["Location"])

    def test_github_start_sets_state_and_redirects(self):
        r = self.anon.get("/api/auth/github/start")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].startswith("https://github.com/login/oauth/authorize?"))
        self.assertIn("gd_state=", r.headers["Set-Cookie"])

    def test_github_callback_rejects_a_missing_or_forged_state(self):
        fresh = api.app.test_client()
        r = fresh.get("/api/auth/github/callback?code=abc&state=forged")
        self.assertTrue(r.headers["Location"].endswith("/#signin?error=github"))

    def test_github_round_trip_signs_in(self):
        start = self.anon.get("/api/auth/github/start")
        state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]
        with mock.patch.object(api.requests, "post", lambda url, **kw: FakeJson({"access_token": "tok"})), \
             mock.patch.object(api.requests, "get", lambda url, **kw: FakeJson(
                 {"id": 42, "login": "collinxweb", "name": "Collins", "avatar_url": "https://x/a.png"})):
            back = self.anon.get("/api/auth/github/callback?code=abc&state=" + state)
        self.assertEqual(back.status_code, 302)
        self.assertTrue(back.headers["Location"].endswith("/#home"))
        self.assertEqual(self.anon.get("/api/me").get_json()["user"]["login"], "collinxweb")

    def test_failed_token_exchange_returns_to_sign_in(self):
        start = self.anon.get("/api/auth/github/start")
        state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]
        with mock.patch.object(api.requests, "post", lambda url, **kw: FakeJson({})):
            back = self.anon.get("/api/auth/github/callback?code=abc&state=" + state)
        self.assertTrue(back.headers["Location"].endswith("/#signin?error=github"))

    def test_google_round_trip_signs_in(self):
        start = self.anon.get("/api/auth/google/start")
        loc = start.headers["Location"]
        self.assertTrue(loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        state = parse_qs(urlparse(loc).query)["state"][0]
        with mock.patch.object(api.requests, "post", lambda url, **kw: FakeJson({"access_token": "tok"})), \
             mock.patch.object(api.requests, "get", lambda url, **kw: FakeJson(
                 {"sub": "9", "name": "Collins", "email": "c@example.com", "picture": ""})):
            back = self.anon.get("/api/auth/google/callback?code=abc&state=" + state)
        self.assertTrue(back.headers["Location"].endswith("/#home"))
        self.assertEqual(self.anon.get("/api/me").get_json()["user"]["p"], "google")

    def test_logout_clears_the_cookie(self):
        r = self.client.post("/api/logout")
        self.assertIn("gd_session=", r.headers["Set-Cookie"])
        self.assertIn("Max-Age=0", r.headers["Set-Cookie"])


class AnalysisTests(Base):
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

    def test_access_code_still_works_when_set(self):
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
        self.assertEqual(self.post("/api/report", url="github.com/x/y", branch="main", items=[item]).status_code, 400)
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

    def test_provider_rate_limit_returns_retry_after(self):
        def limited(provider, prompt, key):
            raise g.RateLimited("busy", 12)
        g.complete = limited
        r = self.post("/api/summarize", branch="main", path="app.py")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["retry_after"], 12)

    def test_limit_is_per_signed_in_user(self):
        api.CALLS_PER_USER = 2
        api.LIMITER = api.Limiter()
        self.assertEqual(self.post("/api/plan").status_code, 200)
        self.assertEqual(self.post("/api/plan").status_code, 200)
        self.assertEqual(self.post("/api/plan").status_code, 429)
        other = api.app.test_client()
        other.set_cookie("gd_session", self.session(id="99", login="someone"))
        self.assertEqual(self.post("/api/plan", client=other).status_code, 200)

    def test_plan_respects_the_scan_limit(self):
        self.assertEqual(self.post("/api/plan", max_files=1).get_json()["files"], ["README.md", "app.py"])
        self.assertEqual(len(self.post("/api/plan", max_files=3).get_json()["files"]), 2)
        self.assertEqual(len(self.post("/api/plan", max_files="lots").get_json()["files"]), 2)
        self.assertEqual(len(self.post("/api/plan", max_files=999).get_json()["files"]), 2)

    def test_routes_work_with_and_without_api_prefix(self):
        self.assertEqual(self.client.get("/config").status_code, 200)
        self.assertEqual(self.post("/plan").status_code, 200)


if __name__ == "__main__":
    unittest.main()
