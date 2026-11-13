# GitDigest

Turn any public GitHub repo into a summary where every claim cites `path:start-end`. Each citation is checked against the file's real line count, and any that don't hold up are marked `[unverified]`.

Pick a purpose: security review, due diligence, onboarding, or a custom focus. Runs on a free Gemini or Groq key.

## How it works on Vercel

Vercel runs short, stateless functions, so the page drives the analysis and each API call does one small job:

1. `POST /api/plan` lists the repo's files through GitHub's API and picks up to 25 (README and manifests first).
2. `POST /api/summarize` summarizes one file. The page calls it once per file, waiting between calls to respect free-tier limits.
3. `POST /api/report` writes the final report and verifies every citation.

Each file summary is signed by the server. `/api/report` rejects any summary that was edited or came from another repo, so the report can only be built from real, server-made summaries. The page also caches file summaries in the visitor's browser, so running a second purpose on the same repo skips the per-file calls.

## Files

| File | What it does |
|---|---|
| `api/index.py` | The three endpoints, access code, rate limit, signing |
| `gitdigest.py` | Core logic and a CLI (file selection, prompts, citation check) |
| `public/index.html` | The mobile-first page |
| `vercel.json` | Routes `/api/*` to the function and sets its time limit |
| `test_gitdigest.py`, `test_api.py` | 30 tests, none call a model or GitHub |

## Deploy

1. Push this folder to your GitHub repo.
2. On vercel.com: Add New, then Project, import the repo. Leave the framework preset as "Other" and deploy.
3. In Project Settings, then Environment Variables, add:
   - `GEMINI_API_KEY` and/or `GROQ_API_KEY`
   - `ACCESS_CODE`, so only people with the code can spend your quota
   - `GITHUB_TOKEN`, a token with no scopes. Vercel's servers share IPs, and GitHub's anonymous limit (60 requests an hour per IP) runs out quickly without it.
4. Redeploy so the variables take effect, then open the project URL.

If a deploy or the first run fails, read the build log and the function log in Vercel's dashboard. The Vercel settings in `vercel.json` (route rewrite, `maxDuration`, `includeFiles`) are written from memory, so check them against Vercel's current docs if the build complains.

## If a run fails with a model error

Providers retire models. If the page says the provider "doesn't recognize the model", set `GEMINI_MODEL` or `GROQ_MODEL` in your host's environment variables to a current model name, then redeploy. As of November 2026 the defaults are `gemini-3.8-flash` and `openai/gpt-oss-120b`. Check Google AI Studio's and Groq's model pages for the current free models.

## Protect your free quota

- **`ACCESS_CODE`** is the real guard. Set it.
- **`CALLS_PER_IP`** (default 90 model calls per visitor per hour, about 3 analyses) is best-effort only. Serverless instances don't share memory, so one visitor can exceed it by landing on different instances.
- Your provider's own free-tier limit is the final cap. When it is hit, the page waits and retries a few times, then shows an error.

## What it refuses

- Only plain `github.com/owner/repo` URLs. Private repos and repos over about 150 MB are refused.
- Branch and file paths from the page are validated, and files are only ever fetched from `raw.githubusercontent.com` for that repo.
- Symlinks and submodules are skipped. Files over 200 KB and binary files are skipped.
- Repo contents are treated as untrusted data in the prompts, and report text is escaped before it is shown.

## CLI

    pip install -r requirements.txt
    export GEMINI_API_KEY="your-key"

    python gitdigest.py github.com/owner/repo --purpose security --dry-run
    python gitdigest.py ./myrepo --purpose onboarding --provider groq
    python gitdigest.py ./myrepo --purpose custom --focus "how user funds move through this code"

The CLI uses `git` (clone or a local repo). `--dry-run` lists the files it would read and the number of model calls, using no quota.

## Tests

    python -m unittest test_gitdigest test_api -v

## Known limits

- Summaries only. It does not reproduce code.
- Gemini reads up to 1,500 lines per file and Groq up to 300, so a citation past that point is marked unverified.
- A verified citation means the file and line range exist. It does not prove the claim is true, so open the link before relying on it.
- Each API call must finish inside the function time limit (60 seconds in `vercel.json`; check what your Vercel plan allows). Very slow model responses can fail.
- No sign-in. GitHub or Google login would need an OAuth app you create.
- Summary history and the file cache live in the visitor's own browser.
