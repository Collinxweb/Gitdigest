"""GitDigest API for Vercel. Every endpoint is stateless and short.

Sign-in uses GitHub or Google OAuth and a signed, HttpOnly session cookie.
Analysis endpoints require that cookie. The page drives an analysis:
  POST /api/plan       list the repo's files and choose which to read
  POST /api/summarize  summarize one file (called once per file)
  POST /api/report     write the final report from signed file summaries
"""
import base64
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
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
from flask import Flask, jsonify, make_response, redirect, request  # noqa: E402

import gitdigest as g  # noqa: E402

# Keep each model call inside the serverless time limit.
g.RETRY_BUDGET = float(os.environ.get("LLM_RETRY_BUDGET", "40"))
g.REQUEST_TIMEOUT = float(os.environ.get("LLM_REQUEST_TIMEOUT", "45"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 400 * 1024

APP_URL = os.environ.get("APP_URL", "https://gitdigest1.vercel.app").rstrip("/")
SECURE_COOKIES = APP_URL.startswith("https://")
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")
CALLS_PER_USER = int(os.environ.get("CALLS_PER_IP", "90"))  # model calls per signed-in user per hour
SESSION_DAYS = 7
KEY_ENV = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}
GITHUB_OAUTH = ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET")
GOOGLE_OAUTH = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
MAX_SUMMARY_CHARS = 8000


class Forbidden(Exception):
    pass


class Unauthorized(Exception):
    pass


class Unavailable(Exception):
    pass


class Limiter:
    """Best-effort per-user counter. Serverless instances don't share memory, so this is not a hard cap."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hits = defaultdict(deque)

    def allow(self, key):
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= CALLS_PER_USER:
                return False
            q.append(now)
            return True


LIMITER = Limiter()


def route(path, **kw):
    """Register under /api<path> and <path>, so it works however the platform forwards the URL."""
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
        except Unauthorized as e:
            return jsonify(error=str(e)), 401
        except Forbidden as e:
            return jsonify(error=str(e)), 403
        except Unavailable as e:
            return jsonify(error=str(e)), 503
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


def login_enabled(pair):
    return all(os.environ.get(k) for k in pair)


# ---------------------------------------------------------------- signed tokens

def _b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _session_secret():
    secret = os.environ.get("SESSION_SECRET", "")
    if len(secret) < 16:
        raise Unavailable("Sign-in isn't set up on this server yet.")
    return secret.encode()


def sign_token(payload):
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    mac = hmac.new(_session_secret(), body.encode(), hashlib.sha256).digest()
    return body + "." + _b64e(mac)


def read_token(token, kind):
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(_session_secret(), body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64e(expected), sig):
        return None
    try:
        data = json.loads(_b64d(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("k") != kind or float(data.get("exp", 0)) < time.time():
        return None
    return data


def current_user():
    data = read_token(request.cookies.get("gd_session"), "session")
    if not data:
        return None
    return {k: str(data.get(k, "")) for k in ("p", "id", "name", "login", "avatar")}


def require_user():
    user = current_user()
    if not user:
        raise Unauthorized("Please sign in to continue.")
    return user


def fail(code):
    return redirect(f"{APP_URL}/#signin?error={code}")


def finish_login(user):
    resp = redirect(APP_URL + "/#home")
    resp.set_cookie("gd_session", sign_token({"k": "session", "exp": time.time() + SESSION_DAYS * 86400, **user}),
                    max_age=SESSION_DAYS * 86400, httponly=True, secure=SECURE_COOKIES, samesite="Lax", path="/")
    resp.delete_cookie("gd_state", path="/")
    return resp


def start_oauth(provider, authorize_url, pair, extra):
    if not login_enabled(pair) or not os.environ.get("SESSION_SECRET"):
        return fail(provider + "_unavailable")
    state = sign_token({"k": "state", "p": provider, "exp": time.time() + 600, "n": os.urandom(8).hex()})
    params = {"client_id": os.environ[pair[0]], "redirect_uri": f"{APP_URL}/api/auth/{provider}/callback",
              "state": state, **extra}
    resp = redirect(authorize_url + "?" + urlencode(params))
    resp.set_cookie("gd_state", state, max_age=600, httponly=True, secure=SECURE_COOKIES, samesite="Lax", path="/")
    return resp


def state_ok(provider):
    given = request.args.get("state", "")
    expected = request.cookies.get("gd_state", "")
    if not given or not expected or not hmac.compare_digest(expected, given):
        return False
    data = read_token(given, "state")
    return bool(data and data.get("p") == provider)


def signing_secret():
    parts = [os.environ.get(e, "") for e in KEY_ENV.values()] + [ACCESS_CODE, "gitdigest-v1"]
    return hashlib.sha256("|".join(parts).encode()).digest()


def sign(repo, branch, item):
    payload = json.dumps([repo, branch, item["path"], item["lines"], item["summary"]], separators=(",", ":"))
    return hmac.new(signing_secret(), payload.encode(), hashlib.sha256).hexdigest()


def common(spend_call=True):
    """Check sign-in and validate the fields every analysis endpoint shares."""
    user = require_user()
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
    if spend_call and not LIMITER.allow(f"{user['p']}:{user['id']}"):
        raise g.RateLimited("You've reached the hourly limit. Try again later.", 0)
    return body, parsed[0], parsed[1], provider, os.environ[KEY_ENV[provider]], user


# ---------------------------------------------------------------- sign-in

@route("/auth/github/start")
def github_start():
    return start_oauth("github", "https://github.com/login/oauth/authorize", GITHUB_OAUTH,
                       {"scope": "read:user", "allow_signup": "true"})


@route("/auth/github/callback")
def github_callback():
    if request.args.get("error") or not request.args.get("code") or not state_ok("github"):
        return fail("github")
    try:
        tok = requests.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"},
                            data={"client_id": os.environ[GITHUB_OAUTH[0]],
                                  "client_secret": os.environ[GITHUB_OAUTH[1]],
                                  "code": request.args["code"],
                                  "redirect_uri": f"{APP_URL}/api/auth/github/callback"},
                            timeout=15).json().get("access_token")
        if not tok:
            return fail("github")
        u = requests.get("https://api.github.com/user", timeout=15,
                         headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                  "User-Agent": "gitdigest"}).json()
    except (requests.RequestException, ValueError):
        return fail("github")
    if "id" not in u:
        return fail("github")
    return finish_login({"p": "github", "id": str(u["id"]), "name": u.get("name") or u.get("login", ""),
                         "login": u.get("login", ""), "avatar": u.get("avatar_url", "")})


@route("/auth/google/start")
def google_start():
    return start_oauth("google", "https://accounts.google.com/o/oauth2/v2/auth", GOOGLE_OAUTH,
                       {"response_type": "code", "scope": "openid email profile", "prompt": "select_account"})


@route("/auth/google/callback")
def google_callback():
    if request.args.get("error") or not request.args.get("code") or not state_ok("google"):
        return fail("google")
    try:
        tok = requests.post("https://oauth2.googleapis.com/token", timeout=15,
                            data={"code": request.args["code"], "grant_type": "authorization_code",
                                  "client_id": os.environ[GOOGLE_OAUTH[0]],
                                  "client_secret": os.environ[GOOGLE_OAUTH[1]],
                                  "redirect_uri": f"{APP_URL}/api/auth/google/callback"}).json().get("access_token")
        if not tok:
            return fail("google")
        info = requests.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=15,
                            headers={"Authorization": f"Bearer {tok}"}).json()
    except (requests.RequestException, ValueError):
        return fail("google")
    if "sub" not in info:
        return fail("google")
    return finish_login({"p": "google", "id": str(info["sub"]), "name": info.get("name") or info.get("email", ""),
                         "login": info.get("email", ""), "avatar": info.get("picture", "")})


@route("/me")
def me():
    return jsonify(user=current_user())


@route("/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify(ok=True))
    resp.delete_cookie("gd_session", path="/")
    return resp


# ---------------------------------------------------------------- app

@route("/config")
def config():
    return jsonify(
        providers=providers(),
        needs_code=bool(ACCESS_CODE),
        max_focus=g.MAX_FOCUS_CHARS,
        gaps={p: g.GAPS[p] for p in KEY_ENV},
        analyses_per_hour=max(1, CALLS_PER_USER // (g.MAX_FILES + 1)),
        login={"github": login_enabled(GITHUB_OAUTH), "google": login_enabled(GOOGLE_OAUTH)},
        login_ready=len(os.environ.get("SESSION_SECRET", "")) >= 16
        and (login_enabled(GITHUB_OAUTH) or login_enabled(GOOGLE_OAUTH)),
    )


@route("/plan", methods=["POST"])
@guarded
def plan():
    body, owner, name, _provider, _key, _user = common()
    g.purpose_instructions(str(body.get("purpose", "")), str(body.get("focus", "")))
    tree = g.github_tree(owner, name)
    return jsonify(repo=f"{owner}/{name}", branch=tree["branch"],
                   tree_sha=tree["tree_sha"], files=tree["files"])


@route("/summarize", methods=["POST"])
@guarded
def summarize():
    body, owner, name, provider, key, _user = common()
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
    body, owner, name, provider, key, _user = common()
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
