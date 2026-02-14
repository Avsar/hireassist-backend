# Hire Assist

Netherlands tech job aggregator. Pulls listings from ATS APIs (Greenhouse, Lever, SmartRecruiters, Recruitee) and scrapes career pages into a single searchable UI.

> **Alpha** -- coverage is incomplete. Not all Dutch tech companies are indexed yet.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 2. Set up environment
cp .env.example .env
# Edit .env if needed (defaults work for local dev)

# 3. Start the web UI
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

On first start, if the database is empty, the app auto-seeds 38 companies from `companies_seed.csv` (committed). No manual seeding step required. To expand beyond the starter set, run `python discover.py` or `python agent_discover.py --region Amsterdam`.

Open [http://localhost:8000/ui](http://localhost:8000/ui) in your browser.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `CORS_ORIGINS` | No | Comma-separated allowed origins. Default: localhost only |
| `DB_PATH` | No | SQLite database path. Default: `companies.db` |
| `PORT` | No | Server port. Render sets this automatically |
| `GIT_SHA` | No | Git commit hash shown in `/version` |
| `BUILD_TIME` | No | Build timestamp shown in `/version` |
| `ANTHROPIC_API_KEY` | No | Only needed for `agent_discover.py --use-ai-cleanup` |

See [.env.example](.env.example) for a template.

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /ui` | Main job search UI (HTML) |
| `GET /ui/momentum` | Top 20 companies by hiring momentum (HTML) |
| `GET /jobs` | Job listings (JSON). Supports `?company=`, `?keyword=`, `?country=`, `?city=` |
| `GET /health` | Health check |
| `GET /version` | App version, git SHA, build time |
| `GET /meta/freshness` | DB row counts (companies, jobs, intel) |
| `GET /stats/companies` | Per-company stats sorted by momentum |
| `GET /stats/company/{name}` | Daily time series for one company |
| `GET /stats/summary` | Aggregate job totals |
| `GET /` | Redirect to `/ui` |

## CLI Tools

```bash
# Daily pipeline (discover + scrape + sync + stats)
python daily_intelligence.py

# Scrape career pages only
python agent_scrape.py

# Sync ATS jobs into intelligence table
python sync_ats_jobs.py

# Discover companies via OpenStreetMap (free)
python agent_discover.py --region Amsterdam

# Discover companies via ATS reverse probing
python agent_discover.py --reverse-ats
```

## Architecture

```
discover.py --------+
detect.py ----------+--> companies.db --> app.py --> /ui, /jobs
                    |
agent_discover.py --+        |
  (OSM Overpass)             |
                    agent_scrape.py --> scraped_jobs
                             |
                    sync_ats_jobs.py --> job_intel.py --> jobs table
                                                    --> company_daily_stats
                                                    --> /stats/*, /ui/momentum
```

- **4 ATS platforms:** Greenhouse, Lever, SmartRecruiters, Recruitee
- **Career page scraping:** Playwright with Workday interception, portal detection, JSON-LD + HTML heuristics
- **Company discovery:** Free OSM Overpass API pipeline
- **Intelligence layer:** Persistent job tracking with lifecycle fields, daily stats, momentum scoring

## Deploy on Render (Alpha)

Create a new **Web Service** on [Render](https://render.com) and configure:

| Setting | Value |
|---|---|
| **Build command** | `pip install -r requirements.txt && playwright install chromium` |
| **Start command** | `uvicorn app:app --host 0.0.0.0 --port $PORT` |

Set these environment variables in the Render dashboard:

| Variable | Value |
|---|---|
| `CORS_ORIGINS` | `https://your-app.onrender.com` |
| `DB_PATH` | `companies.db` |
| `GIT_SHA` | *(optional)* set automatically if using Render's `RENDER_GIT_COMMIT` |
| `BUILD_TIME` | *(optional)* |

After deploy, verify:

1. `https://your-app.onrender.com/health` -- should return JSON with `total_jobs`
2. `https://your-app.onrender.com/version` -- should show `"app": "HireAssist Alpha"`
3. `https://your-app.onrender.com/ui` -- should load the job search UI

## Docker

```bash
docker compose up
```

Services: `api` (web UI), `discover` (ATS prober), `scrape` (Playwright scraper), `discover-ai` (OSM pipeline).
