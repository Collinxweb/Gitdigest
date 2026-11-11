import shutil
import time
import unittest
from pathlib import Path

import app as appmod
import gitdigest as g
from test_gitdigest import make_repo


class AppTests(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        self.saved = (appmod.ACCESS_CODE, appmod.PER_IP_LIMIT, appmod.DAILY_LIMIT, g.complete,
                      g.github_repo_info, g.clone_github, dict(g.GAPS))
        g.GAPS.update(gemini=0, groq=0)
        appmod.LIMITER = appmod.Limiter()
        appmod.ACCESS_CODE = ""
        import os
        os.environ["GEMINI_API_KEY"] = "test-key"
        os.environ.pop("GROQ_API_KEY", None)
        self.repo = make_repo({"README.md": "# demo\n", "app.py": "print('hi')\n"})

        g.github_repo_info = lambda o, n: {"default_branch": "main"}
        g.clone_github = lambda o, n, dest: shutil.copytree(self.repo, dest, dirs_exist_ok=True)
        g.complete = lambda p, prompt, key: ("## Overview\nPrints. `app.py:1-1`" if "<purpose>" in prompt
                                              else "Greets. app.py:1-1")

    def tearDown(self):
        (appmod.ACCESS_CODE, appmod.PER_IP_LIMIT, appmod.DAILY_LIMIT, g.complete,
         g.github_repo_info, g.clone_github, saved_gaps) = self.saved
        g.GAPS.update(saved_gaps)

    def post(self, **over):
        body = {"url": "github.com/o/r", "purpose": "security", "focus": ""}
        body.update(over)
        return self.client.post("/api/analyze", json=body)

    def wait(self, job):
        for _ in range(100):
            data = self.client.get(f"/api/jobs/{job}").get_json()
            if data["state"] in ("done", "error"):
                return data
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_pages_and_config(self):
        self.assertEqual(self.client.get("/healthz").data, b"ok")
        self.assertIn(b"GitDigest", self.client.get("/").data)
        cfg = self.client.get("/api/config").get_json()
        self.assertEqual(cfg["providers"], ["gemini"])
        self.assertFalse(cfg["needs_code"])

    def test_full_job_flow(self):
        r = self.post()
        self.assertEqual(r.status_code, 202)
        data = self.wait(r.get_json()["job"])
        self.assertEqual(data["state"], "done", data)
        res = data["result"]
        self.assertEqual((res["repo"], res["branch"]), ("o/r", "main"))
        self.assertEqual((res["citations_ok"], res["citations_bad"]), (1, 0))

    def test_bad_input_is_rejected(self):
        self.assertEqual(self.post(url="https://evil.com/x").status_code, 400)
        self.assertEqual(self.post(purpose="nope").status_code, 400)
        self.assertEqual(self.post(purpose="custom", focus="").status_code, 400)
        self.assertEqual(self.post(provider="groq").status_code, 400)  # no key configured

    def test_access_code(self):
        appmod.ACCESS_CODE = "sesame"
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.post(code="wrong").status_code, 403)
        r = self.post(code="sesame")
        self.assertEqual(r.status_code, 202)
        self.wait(r.get_json()["job"])

    def test_rate_limits(self):
        appmod.PER_IP_LIMIT = 1
        first = self.post()
        self.assertEqual(first.status_code, 202)
        self.wait(first.get_json()["job"])
        self.assertEqual(self.post().status_code, 429)
        appmod.LIMITER = appmod.Limiter()
        appmod.PER_IP_LIMIT, appmod.DAILY_LIMIT = 99, 0
        self.assertEqual(self.post().status_code, 429)

    def test_repo_errors_become_clean_messages(self):
        def boom(o, n):
            raise g.RepoError("Repository not found, or it is private.")
        g.github_repo_info = boom
        data = self.wait(self.post().get_json()["job"])
        self.assertEqual(data["state"], "error")
        self.assertIn("private", data["error"])

    def test_unknown_job(self):
        self.assertEqual(self.client.get("/api/jobs/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
