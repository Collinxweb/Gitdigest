# GitDigest

Turn any public GitHub repo into a summary where every claim cites `path:start-end`. Each citation is checked against the real files, and any that don't hold up are marked `[unverified]`.

Pick a purpose: security review, due diligence, onboarding, or a custom focus. Runs on a free Gemini or Groq key.

## What's in the box

| File | What it does |
|---|---|
| `gitdigest.py` | The pipeline and a CLI: file selection, summaries, cache, citation check |
| `app.py` | Web server: serves the page, runs jobs, rate limits |
| `static/index.html` | The mobile-first UI |
| `Dockerfile`, `render.yaml` | Deploy config (the image includes `git`, which cloning needs) |
| `test_*.py` | 19 tests, none call a model API |

## Deploy on Render (free)

1. Create a GitHub repo and add all these files. On a phone, use GitHub's "Add file → Create new file" and type `static/index.html` as the name to create the folder, or push from Termux with `git`.
2. On Render: New → Blueprint (or Web Service) → pick your repo. It reads `render.yaml` and builds the Dockerfile.
3. In the service's Environment tab, add:
   - `GEMINI_API_KEY` and/or `GROQ_API_KEY`
   - `ACCESS_CODE` (recommended, see below)
4. Deploy. Open the `.onrender.com` URL.

Free Render services sleep when idle, so the first load after a quiet period can take about a minute.

Railway and Fly.io work the same way: they build the Dockerfile and need the same environment variables.

## Protect your free quota

Every analysis spends your model quota, so the server has guards. All are environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `ACCESS_CODE` | none | If set, visitors must enter it. Strongly recommended for a public link. |
| `PER_IP_LIMIT` | 3 | Runs per visitor per hour |
| `DAILY_LIMIT` | 40 | Runs per day across everyone |
| `MAX_CONCURRENT` | 2 | Analyses at once |
| `GITHUB_TOKEN` | none | Optional. Raises GitHub API limits. A token with no scopes is enough. |
| `GEMINI_MODEL`, `GROQ_MODEL` | see `.env.example` | Check these against each provider's current docs |

Counters live in memory and reset when the server restarts. Keep the single gunicorn worker in the Dockerfile, because jobs are stored in that worker's memory.

## What it refuses

- Only plain `github.com/owner/repo` URLs. The clone URL is rebuilt from the validated owner and repo, never taken raw.
- Private repos, and repos larger than about 150 MB.
- Symlinks inside a repo are ignored, so a repo can't point the server at its own files.
- Repo contents are treated as untrusted data in the prompts, and report text is escaped before it is shown.

## CLI

    pip install -r requirements.txt
    export GEMINI_API_KEY="your-key"

    python gitdigest.py github.com/owner/repo --purpose security --dry-run
    python gitdigest.py ./myrepo --purpose onboarding --provider groq
    python gitdigest.py ./myrepo --purpose custom --focus "how user funds move through this code"

`--dry-run` lists the files it would read and the number of model calls, using no quota.

## Tests

    python -m unittest test_gitdigest test_app -v

## Known limits

- Summaries only. It does not reproduce code.
- Up to 25 files per run, README and manifests first. Files over 200 KB are skipped.
- Gemini reads up to 1,500 lines per file and Groq up to 300, so a citation past that point is marked unverified.
- A verified citation means the file and line range exist. It does not prove the claim is true, so open the link before relying on it.
- No sign-in. GitHub or Google login would need an OAuth app you create, and isn't included.
- Summaries history is stored in the visitor's own browser, not on the server.
