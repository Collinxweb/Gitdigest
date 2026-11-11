"""GitDigest web server: serves the UI and runs analysis jobs.

Run with a single gunicorn worker (jobs live in memory):
    gunicorn app:app --workers 1 --threads 8 --timeout 120
"""
import hmac
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

import gitdigest as g

log = logging.getLogger("gitdigest")
logging.basicConfig(level=logging.INFO)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=int(os.environ.get("PROXY_HOPS", "1")))
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

ACCESS_CODE = os.environ.get("ACCESS_CODE", "")
PER_IP_LIMIT = int(os.environ.get("PER_IP_LIMIT", "3"))          # runs per IP per hour
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "40"))           # runs per UTC day, all users
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
JOB_TTL = 3600

CACHE = g.Cache(os.environ.get("GITDIGEST_CACHE", "/tmp/gitdigest-cache.json"))
KEY_ENV = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}

JOBS = {}
JOBS_LOCK = threading.Lock()
SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)


class Limiter:
    """In-memory per-IP and daily counters. Resets on restart."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_ip = defaultdict(deque)
        self.day = time.strftime("%Y-%m-%d", time.gmtime())
        self.day_count = 0

    def allow(self, ip):
        now = time.time()
        with self.lock:
            today = time.strftime("%Y-%m-%d", time.gmtime(now))
            if today != self.day:
                self.day, self.day_count = today, 0
            hits = self.by_ip[ip]
            while hits and now - hits[0] > 3600:
                hits.popleft()
            if len(hits) >= PER_IP_LIMIT:
                return False, "You've reached the hourly limit. Try again later."
            if self.day_count >= DAILY_LIMIT:
                return False, "The daily limit for this service has been reached. Try again tomorrow."
            hits.append(now)
            self.day_count += 1
            return True, ""


LIMITER = Limiter()


def available_providers():
    return [p for p, env in KEY_ENV.items() if os.environ.get(env)]


def purge_jobs():
    cutoff = time.time() - JOB_TTL
    with JOBS_LOCK:
        for jid in [j for j, v in JOBS.items() if v["created"] < cutoff]:
            del JOBS[jid]


def run_job(job_id, owner, name, purpose, focus, provider):
    job = JOBS[job_id]
    tmp = None

    def progress(message, done, total):
        job.update(message=message, done=done, total=total)

    try:
        job.update(message="Checking the repository", state="running")
        info = g.github_repo_info(owner, name) or {}
        tmp = tempfile.mkdtemp(prefix="gitdigest-")
        job.update(message="Cloning the repository")
        g.clone_github(owner, name, tmp)
        result = g.analyze(Path(tmp), purpose, focus, provider,
                           os.environ[KEY_ENV[provider]], CACHE, progress)
        result["repo"] = f"{owner}/{name}"
        result["branch"] = info.get("default_branch") or "HEAD"
        job.update(state="done", result=result, message="Done")
    except (g.InputError, g.RepoError, g.LLMError) as e:
        job.update(state="error", error=str(e))
    except Exception:
        log.exception("job %s failed", job_id)
        job.update(state="error", error="Something went wrong while analyzing that repository.")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        SLOTS.release()


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/config")
def config():
    return jsonify(
        providers=available_providers(),
        needs_code=bool(ACCESS_CODE),
        max_focus=g.MAX_FOCUS_CHARS,
        per_ip_limit=PER_IP_LIMIT,
    )


@app.post("/api/analyze")
def analyze():
    body = request.get_json(silent=True) or {}
    if ACCESS_CODE and not hmac.compare_digest(str(body.get("code", "")), ACCESS_CODE):
        return jsonify(error="That access code isn't right."), 403

    parsed = g.parse_github_url(str(body.get("url", "")))
    if not parsed:
        return jsonify(error="Enter a public GitHub repo URL like github.com/owner/repo."), 400

    providers = available_providers()
    provider = str(body.get("provider") or (providers[0] if providers else ""))
    if provider not in providers:
        return jsonify(error="That model provider isn't available on this server."), 400

    purpose = str(body.get("purpose", ""))
    focus = str(body.get("focus", ""))
    try:
        g.purpose_instructions(purpose, focus)
    except g.InputError as e:
        return jsonify(error=str(e)), 400

    purge_jobs()
    allowed, reason = LIMITER.allow(request.remote_addr or "unknown")
    if not allowed:
        return jsonify(error=reason), 429
    if not SLOTS.acquire(blocking=False):
        return jsonify(error="The server is busy with other analyses. Try again in a minute."), 429

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"created": time.time(), "state": "queued", "message": "Queued",
                    "done": 0, "total": 1}
    threading.Thread(target=run_job, daemon=True,
                     args=(job_id, *parsed, purpose, focus, provider)).start()
    return jsonify(job=job_id), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="That job has expired or doesn't exist."), 404
    out = {k: job[k] for k in ("state", "message", "done", "total") if k in job}
    if job["state"] == "done":
        out["result"] = job["result"]
    if job["state"] == "error":
        out["error"] = job["error"]
    return jsonify(out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
