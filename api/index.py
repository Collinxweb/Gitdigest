"""GitDigest API for Vercel. Every endpoint is stateless and short.

The page drives the analysis:
  POST /api/plan       list the repo's files and choose which to read
  POST /api/summarize  summarize one file (called once per file)
  POST /api/report     write the final report from signed file summaries
"""
import functools
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, request  # noqa: E402

import gitdigest as g  # noqa: E402

# Keep each model call inside the serverless time limit.
g.RETRY_BUDGET = float(os.environ.get("LLM_RETRY_BUDGET", "40"))
g.REQUEST_TIMEOUT = float(os.environ.get("LLM_REQUEST_TIMEOUT", "45"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 400 * 1024

ACCESS_CODE = os.environ.get("ACCESS_CODE", "")
CALLS_PER_IP = int(os.environ.get("CALLS_PER_IP", "90"))  # model calls per visitor per hour
KEY_ENV = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}
MAX_SUMMARY_CHARS = 8000


class Forbidden(Exception):
    pass


class Limiter:
    """Best-effort per-visitor counter. Serverless instances don't share memory, so this is not a hard cap."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hits = defaultdict(deque)

    def allow(self, ip):
        now = time.time()
        with self.lock:
            q = self.hits[ip]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= CALLS_PER_IP:
                return False
            q.append(now)
            return True


LIMITER = Limiter()


def route(path, **kw):
    """Register under /api/<path> and /<path>, so it works however the platform forwards the URL."""
    def deco(fn):
        app.add_url_rule("/api" + path, fn.__name__ + "_api", fn, **kw)
        app.add_url_rule(path, fn.__name__ + "_bare", fn, **kw)
        return fn
    return deco


def guarded(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Forbidden as e:
            return jsonify(error=str(e)), 403
        except g.InputError as e:
            return jsonify(error=str(e)), 400
        except g.RepoError as e:
            return jsonify(error=str(e)), 400
        except g.RateLimited as e:
            return jsonify(error=str(e), retry_after=round(e.retry_after)), 429
        except g.LLMError as e:
            return jsonify(error=str(e)), 502
        except Exception:
            app.logger.exception("unhandled error")
            return jsonify(error="Something went wrong. Please try again."), 500
    return wrapper


def providers():
    return [p for p, env in KEY_ENV.items() if os.environ.get(env)]


def client_ip():
    return (request.headers.get("x-vercel-forwarded-for") or request.headers.get("x-real-ip")
            or request.remote_addr or "unknown").split(",")[0].strip()


def signing_secret():
    parts = [os.environ.get(e, "") for e in KEY_ENV.values()] + [ACCESS_CODE, "gitdigest-v1"]
    return hashlib.sha256("|".join(parts).encode()).digest()


def sign(repo, branch, item):
    payload = json.dumps([repo, branch, item["path"], item["lines"], item["summary"]], separators=(",", ":"))
    return hmac.new(signing_secret(), payload.encode(), hashlib.sha256).hexdigest()


def common(spend_call=True):
    """Validate the fields every endpoint shares. Returns (body, owner, name, provider, api_key)."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise g.InputError("Invalid request.")
    if ACCESS_CODE and not hmac.compare_digest(str(body.get("code", "")), ACCESS_CODE):
        raise Forbidden("That access code isn't right.")
    parsed = g.parse_github_url(str(body.get("url", "")))
    if not parsed:
        raise g.InputError("Enter a public GitHub repo URL like github.com/owner/repo.")
    available = providers()
    provider = str(body.get("provider") or (available[0] if available else ""))
    if provider not in available:
        raise g.InputError("That model provider isn't available on this server.")
    if spend_call and not LIMITER.allow(client_ip()):
        raise g.RateLimited("You've reached the hourly limit. Try again later.", 0)
    return body, parsed[0], parsed[1], provider, os.environ[KEY_ENV[provider]]


@route("/config")
def config():
    return jsonify(
        providers=providers(),
        needs_code=bool(ACCESS_CODE),
        max_focus=g.MAX_FOCUS_CHARS,
        gaps={p: g.GAPS[p] for p in KEY_ENV},
        analyses_per_hour=max(1, CALLS_PER_IP // (g.MAX_FILES + 1)),
    )


@route("/plan", methods=["POST"])
@guarded
def plan():
    body, owner, name, _provider, _key = common()
    g.purpose_instructions(str(body.get("purpose", "")), str(body.get("focus", "")))
    tree = g.github_tree(owner, name)
    return jsonify(repo=f"{owner}/{name}", branch=tree["branch"],
                   tree_sha=tree["tree_sha"], files=tree["files"])


@route("/summarize", methods=["POST"])
@guarded
def summarize():
    body, owner, name, provider, key = common()
    branch, path = str(body.get("branch", "")), str(body.get("path", ""))
    try:
        text = g.fetch_raw(owner, name, branch, path)
    except g.RepoError as e:
        return jsonify(error=str(e), skip=True), 422  # one unreadable file shouldn't stop the run
    lines = text.splitlines()[:g.MAX_SHOWN_LINES[provider]]
    summary = g.complete(provider, g.FILE_PROMPT.format(path=path, numbered=g.number_lines(lines)), key)
    item = {"path": path, "lines": len(lines), "summary": summary}
    item["sig"] = sign(f"{owner}/{name}", branch, item)
    return jsonify(item)


@route("/report", methods=["POST"])
@guarded
def report():
    body, owner, name, provider, key = common()
    instructions = g.purpose_instructions(str(body.get("purpose", "")), str(body.get("focus", "")))
    branch = str(body.get("branch", ""))
    items = body.get("items")
    if not isinstance(items, list) or not items or len(items) > g.MAX_FILES:
        raise g.InputError("Invalid file list.")

    repo = f"{owner}/{name}"
    summaries, line_counts = [], {}
    for it in items:
        ok_shape = (isinstance(it, dict) and g.valid_repo_path(it.get("path"))
                    and isinstance(it.get("lines"), int) and not isinstance(it.get("lines"), bool)
                    and isinstance(it.get("summary"), str) and len(it["summary"]) <= MAX_SUMMARY_CHARS
                    and isinstance(it.get("sig"), str))
        if not ok_shape:
            raise g.InputError("Invalid file list.")
        if not hmac.compare_digest(sign(repo, branch, it), it["sig"]):
            return jsonify(error="Saved summaries are out of date. Run the analysis again.", stale=True), 400
        summaries.append(f"### {it['path']}\n{it['summary']}")
        line_counts[it["path"]] = it["lines"]

    result = g.finish_report(provider, key, instructions, summaries, line_counts, list(line_counts))
    result.update(purpose=str(body.get("purpose")), provider=provider, repo=repo, branch=branch)
    return jsonify(result)
