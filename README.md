# Financial Reconciliation Tool

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-2.3-000000?logo=flask&logoColor=white)
![Databricks](https://img.shields.io/badge/Databricks-Unity%20Catalog%20%7C%20Delta%20%7C%20Spark-FF3621?logo=databricks&logoColor=white)
![Chart.js](https://img.shields.io/badge/Chart.js-4.6-FF6384?logo=chartdotjs&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-informational)

Automated GL-to-bank reconciliation with a 3-pass mutual-best-match engine,
behind one Flask app that runs identically against a local SQLite database or
a full Databricks pipeline (Unity Catalog, Delta Lake, Auto Loader, a Spark
job) — flip one environment variable, same UI, same API, no code changes.

**Tested at 500,000+ GL rows locally (99.99% precision / 96.6% recall) and
at 1,000,000 rows on a live Databricks job (98.9% precision / 95.6% recall).**

## Why this exists

The original version was a solid local Flask + SQLite reconciliation tool.
This build keeps every bit of that working exactly as before, and adds a
second, swappable backend that runs the same matching logic as a real
cloud data pipeline — Auto Loader ingestion into Bronze, PySpark cleaning
into Silver, a 3-pass matcher producing Gold tables, with idempotent Delta
`MERGE` so re-running the pipeline doesn't lose manually-resolved exceptions.
It's meant to show both ends: a working product, and the data-engineering
underneath it.

## Quickstart

```bash
git clone <this-repo-url>
cd financial-reconciliation
./run_local.sh      # SQLite mode, zero config
```
Open http://localhost:5000, grab the sample GL/bank CSVs from the Upload
tab, and click through Upload → Reconcile → Dashboard.

---

One Flask app, two interchangeable backends:

| | `RECON_BACKEND` unset (default) | `RECON_BACKEND=databricks` |
|---|---|---|
| Storage | local SQLite file | Unity Catalog Delta tables |
| Matching | runs in-process (Pandas loops) | a Databricks job (Spark, 3-pass mutual-best-match) |
| Scale | good for demos, ~hundreds-thousands of rows | tested to 500,000+ rows |
| Setup | none — just `python app.py` | a Databricks Free Edition workspace + a few env vars |

The frontend (`frontend/`) and the API contract are identical either way — the
same `index.html`/`script.js` talk to the same `/api/...` routes, and get back
the same field names, whichever backend is running. You flip between them with
one environment variable, not by editing code.

## Project layout

```
backend/
  app.py                  thin Flask layer: routes call backend.<fn>(), picks the
                           module via RECON_BACKEND, loads .env via python-dotenv
  sqlite_backend.py        your original engine, refactored into functions
                            (one real bug fixed — see below)
  databricks_backend.py    same functions, backed by a Databricks SQL warehouse
                            + a Databricks job, for when RECON_BACKEND=databricks
  reconciliation_engine.py  your original matching algorithms (date-parsing fixed)
  sample_data.py            your original sample GL/bank generator
  requirements.txt
databricks/
  recon_lib.py              shared PySpark logic for the Databricks path
  01_generate_data.py .. 04_gold_reconcile.py   the pipeline notebooks
  05_dashboard_queries.sql  queries for a Databricks SQL dashboard
frontend/
  index.html                your original page, plus one new <span> for the
                             backend indicator badge (see below)
  static/style.css          your original styles, plus badge/auto-resolved styling
  static/script.js          your original script, with one bug fix (see below)
.env.example                copy to .env and fill in for Databricks mode
.gitignore
run_local.sh                 pip install + run against SQLite, no setup needed
run_databricks.sh            pip install + run against Databricks (needs .env)
```

## Running locally (no Databricks needed)

```bash
./run_local.sh
```
(or manually: `cd backend && pip install -r requirements.txt && python app.py`)

Nothing to configure — this is your original app, working exactly as before.
Open http://localhost:5000; the header now shows a small "💻 Running locally
(SQLite)" badge so it's obvious which mode is live.

## Running against Databricks

1. Follow the Databricks setup steps already covered (sign up for Free
   Edition, import the `databricks/*.py` notebooks, run `01` once to seed
   data, then set up `02`→`03`→`04` as a Workflows job).
2. Note the job's id (Workflows → Jobs → your job → the number in the URL).
3. Note your SQL warehouse's HTTP path (SQL Warehouses → your warehouse →
   Connection details → "HTTP path").
4. Create a personal access token (Settings → Developer → Access tokens).
5. Copy `.env.example` to `.env` and fill in the five values above.
6. Run:

```bash
./run_databricks.sh
```

Open http://localhost:5000 — same UI, same buttons, now showing "⚡ Running
on Databricks". Uploading a CSV now writes it to the Databricks landing
volume instead of SQLite; clicking "Run Reconciliation" now triggers the real
job and waits for it to finish (seconds to a few minutes, rather than
instant — there's a spinner now so a longer wait doesn't look like it hung).

## What changed in your original code, and why

Two things were fixed, both confirmed by tests (below), not just reasoning:

1. **Date parsing silently broke every date-window check.** `upload_gl`/
   `upload_bank` store dates as `str(pd.to_datetime(...))`, which produces
   `"2024-01-15 00:00:00"`. The original `_parse_date` only tried date-only
   formats against that full string, failed every time, and fell back to
   `datetime.now()` — so the 3-day and 5-day match windows were silently
   comparing against *today* for every entry, not the transaction date. Fixed
   in `reconciliation_engine.py` by also trying the date part before the full
   string.
2. **No mutual-best-match guarantee**, carried over from the original engine:
   a popular bank row could in principle be claimed by more than one GL
   entry. This is fixed in the Databricks path (`recon_lib.py`'s
   `_resolve()`), but **not** in the local SQLite engine, to keep that one a
   faithful refactor rather than a rewrite. If you want it fixed there too,
   say so and it's a contained change.
3. **`script.js`'s resolve button would have broken against the Databricks
   backend.** Databricks exceptions need a composite string id (e.g.
   `"GL:JE00042"`) to stay stable across pipeline reruns — a plain integer
   like SQLite uses isn't enough there. The original `resolveException(${exc.id})`
   embeds that id *unquoted* into the button's `onclick`, which is valid only
   for a bare number; with a colon in it, the browser would throw
   `SyntaxError: missing ) after argument list` the moment the Exceptions tab
   rendered a row (confirmed this by actually running the generated
   `onclick` string through Node's `Function` constructor, both before and
   after the fix). Fixed by quoting the id in the template string. Harmless,
   and no behavior change, for the SQLite backend's numeric ids.

## Tests included (all run, all passing)

- `test_app_sqlite.py` — drives the Flask app end-to-end through the SQLite
  backend (upload → reconcile → summary → matches → exceptions → resolve →
  export) and confirms the date-parsing fix actually changes behavior.
- `test_databricks_backend_logic.py` — exercises `databricks_backend.py`'s
  Python logic (field renaming, response shaping) with the network calls
  mocked out. This does **not** touch a live Databricks workspace (no
  credentials available here) — it only proves the code doesn't crash and
  returns the field names the frontend expects.
- `test_local.py` / `test_merge_logic.py` — the Databricks pipeline's own
  tests (accuracy at scale, and the resolve-state-survives-reruns MERGE
  behavior), carried over from the first zip.

The resolve-button fix above was checked directly with Node (not just by
reasoning about it): the exact row-rendering logic from `script.js` was run
with a Databricks-style id, the resulting `onclick` string was fed through
`new Function(...)`, and it threw `SyntaxError: missing ) after argument
list` before the fix and ran clean after it.

Run the Python tests with `python <name>.py` from this folder (needs `pip
install pyspark` for `test_local.py`/`test_merge_logic.py`, Flask already in
requirements.txt for the rest).

## UI: dark "developer tool" redesign

`frontend/index.html`, `frontend/static/style.css`, and `frontend/static/script.js`
were reworked for a dark, monospace-accented look (JetBrains Mono for
numbers/code, Inter for body text) instead of the original light theme. The
API contract, tab structure, and every existing feature are unchanged — this
is a visual pass, not a rewrite. Two real bugs were caught and fixed in the
process (not just restyled around):

- The dashboard's Chart.js doughnut chart had no height constraint on its
  container, so it rendered as a near-1200px circle filling the whole card.
  Fixed with a `.chart-canvas-wrap` wrapper (fixed height, `position:
  relative`) plus `maintainAspectRatio: false` in `updateStatusChart()`.
- Chart.js was loaded from `cdn.jsdelivr.net`. It's vendored locally now
  (`frontend/static/chart.umd.min.js`, Chart.js 4.6.1) so the app has no
  external runtime dependency once deployed — it won't break behind a
  corporate firewall or if that CDN has an outage.

## Deploying it publicly

The frontend is unchanged by where you deploy it, so any host that runs a
Python/Flask app works. **Render** is the easiest free option and is what
`render.yaml` and the `Procfile` here are set up for:

1. Push this folder to a GitHub repo.
2. On Render: **New +** → **Blueprint**, point it at the repo. It reads
   `render.yaml` and creates the web service for you (build command installs
   `backend/requirements.txt`, start command runs
   `gunicorn --chdir backend app:app`).
3. In the service's **Environment** tab, set the four `DATABRICKS_*`
   variables (`render.yaml` leaves them blank on purpose — don't put real
   tokens in git). `RECON_BACKEND=databricks` is already set, so the
   deployed app triggers your real Databricks job on every "Run
   Reconciliation" click, the same as `run_databricks.sh` does locally.
4. Render gives you a `https://<name>.onrender.com` URL. That's the whole
   deployment — no further config.

Railway and PythonAnywhere work too (same `gunicorn --chdir backend app:app`
start command on Railway; PythonAnywhere's WSGI config file instead, since
it doesn't use a Procfile), but Render's free tier needs the least setup.

**On running the Databricks backend publicly with no login:** anything at
that URL can upload files and trigger a real Databricks job run — there's no
auth wall in front of it. `backend/app.py` now rate-limits the endpoints
that cost money or write data (`/api/upload-gl`, `/api/upload-bank`: 20/hour
each; `/api/reconcile`, the one that actually launches a Databricks job run:
10/hour) via Flask-Limiter, per IP address, on top of a blanket 200/day,
50/hour default. That stops a stray bot or a refresh-loop from burning your
Databricks compute or filling the Volume; it is not real authentication, and
someone who wants to abuse a public no-login endpoint on purpose still can.
Treat it as a portfolio demo you can reset/redeploy, not as a system holding
real financial data from the public.
