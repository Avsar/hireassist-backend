# Hire Assist — Netherlands Tech Job Aggregator

## Project Summary

A FastAPI web app that aggregates job listings from Dutch tech companies across multiple sources into a single searchable UI at `localhost:8000/ui`.

**Location:** `c:\Users\amita\SourceJobsNL\project`

---

## What We Built (11 components)

### 1. `discover.py` — ATS Token Prober
- Hard-coded list of 145+ NL tech companies
- Probes 4 ATS platforms: Greenhouse, Lever, SmartRecruiters, Recruitee
- Validates API endpoints and stores working tokens in `companies.db`
- Run: `python discover.py` (one-shot)

### 2. `detect.py` — Career Page URL Finder
- Tries standard paths (`/careers`, `/jobs`, `/vacatures`, etc.) on company domains
- Tries subdomain variations (`careers.company.com`)
- Stores career page URLs as fallback for companies without ATS APIs
- Run: `python detect.py --add`

### 3. `app.py` — FastAPI Web Application (~1500 lines)
- Fetches jobs from all 4 ATS APIs in real-time
- Reads scraped jobs from `scraped_jobs` table
- 10-minute in-memory cache
- Filters: company, keyword, country, city, English-only, new-today
- **Redesigned UI (v2):** Inter font, blue gradient hero, search bar, quick filter pills, compact 2-column sidebar, job cards with company initials/meta icons/snippets/Apply+Save buttons
- **Hero section:** Search bar with keyword + city selector, live stats strip (jobs indexed, companies, new today, cities)
- **Quick filters:** Horizontal pills (All, English only, New today, city shortcuts) with instant navigation
- **Sidebar:** Compact 2-column filter grid with auto-submit on dropdowns/checkboxes + live momentum widget (top 5 companies)
- **Job cards:** Company initials logo, location/type meta with SVG icons, description snippets, "New today" / source pills, Save + Apply buttons
- Pagination: 100 jobs per page with numbered pagination
- **Intelligence endpoints:** `/stats/companies`, `/stats/company/{name}`, `/stats/summary`
- **Momentum page:** `/ui/momentum` — top 20 companies by hiring momentum score (matching v2 design)
- **Topbar navigation:** Jobs, Companies, Company Momentum, For Employers, Post a Job
- Endpoints: `/ui` (browser), `/ui/momentum`, `/jobs` (API), `/ping` (keep-alive), `/health`, `/stats/*`, `/`
- Run: `python -m uvicorn app:app --host 0.0.0.0 --port 8000`

### 4. `agent_scrape.py` — Playwright Career Page Scraper (~970 lines)
- Headless Chromium renders JS-heavy career pages
- Extracts jobs via JSON-LD schema + HTML heuristics
- **Workday support:** intercepts `/wday/cxs/` JSON API responses via Playwright network interception; detects Workday by URL, HTML markers, or embedded links in non-Workday pages
- **Global portal detection:** follows redirect chains and detects Greenhouse, Lever, SmartRecruiters, Workday, iCIMS, Taleo, Recruitee, Ashby portals; dispatches to appropriate scraper
- **Enhanced JSON-LD:** parses all `<script type="application/ld+json">` blocks, flattens `@graph` arrays, handles `@type` as list, normalizes nested location fields (`addressRegion`, dict-format `addressCountry`)
- **SPA-aware multi-strategy pipeline:**
  1. Load page with networkidle + fallback wait
  2. Detect portal redirect or Workday markers
  3. Parse initial HTML (enhanced JSON-LD + heuristics)
  4. Check for ATS iframes (Greenhouse, Lever, etc.)
  5. Follow "view all jobs" links to sub-pages
  6. Click "load more" / "show more" buttons
  7. Scroll to trigger lazy loading
- **Safe per-company replacement:** only deletes old jobs when fresh jobs found; preserves existing data on 0-result or failed scrape
- **Intelligence layer hook:** after saving to `scraped_jobs`, also upserts into `jobs` table via `job_intel.py` (guarded import, degrades gracefully)
- **Debug mode:** `--debug-company "Name"` prints final URL, portal type, intercepted API endpoints, job details
- **End-of-run metrics:** prints attempted/success/zero/failed/total counts
- Run: `python agent_scrape.py` or `python agent_scrape.py --dry-run`
- Run (debug): `python agent_scrape.py --debug-company "Vanderlande"`

### 5. `agent_discover.py` — OSM-Based Company Discovery Pipeline (~1050 lines)
- **Free, deterministic pipeline** -- no paid API keys required for core operation
- Discovers companies via OpenStreetMap Overpass API (`osm_discover.py`)
- Scores and filters candidates using OSM tags + name heuristics (`candidate_filter.py`)
- Probes 4 ATS platforms with **board-name verification** to prevent token collision false positives
- Falls back to career page detection with **domain verification** (rejects suspicious redirects)
- Stores validated companies in `companies.db`, tracks all candidates in `discovery_candidates`
- Resumable: reruns skip already-processed candidates (status tracking per OSM ID)
- Supports `--region` for scoping: city (Eindhoven), province (Noord-Brabant), or country (Netherlands)
- Optional `--use-ai-cleanup` flag uses Claude Haiku (~$0.03/run) for name/website normalisation on the filtered batch
- **No DuckDuckGo, no Claude Sonnet agent loop** -- see "Why we removed Claude Sonnet discovery" below
- Run: `python agent_discover.py --region Eindhoven`
- Run (strict): `python agent_discover.py --region Eindhoven --min-score 40 --require-website`
- Run (dry-run): `python agent_discover.py --region Amsterdam --dry-run --limit 50`

### 6. `osm_discover.py` — OpenStreetMap Overpass API Module
- Queries the free Overpass API for offices/companies in a named region
- Resolves region names: Dutch cities, provinces, country, English aliases
- Returns structured candidates: name, website, city, lat/lon, OSM ID, tags
- 740 unique candidates from Eindhoven alone
- Built-in retry with backoff for Overpass rate limits (504s)
- Run standalone: `python osm_discover.py Eindhoven --limit 50`

### 7. `candidate_filter.py` — Candidate Scoring and Filtering
- Deterministic scoring (-100 to +100) based on OSM tags and name heuristics
- Excludes restaurants, shops, schools, churches, salons, hotels, etc.
- Prefers: `office=company`, `office=it`, `industrial=*`, corporate name indicators
- Eligibility gate: must have website OR strong corporate indicator (B.V., technologies, engineering, etc.)
- Imported by `agent_discover.py` -- runs before any HTTP probing

### 8. `job_intel.py` — Job Intelligence Layer (~250 lines)
- **Persistent job tracking:** `jobs` table stores ALL jobs (ATS + scraped) with `first_seen_at`, `last_seen_at`, `is_active` lifecycle fields
- **Stable dedupe keys:** ATS sources use provider IDs; scraped jobs use `sha1(company|title|normalized_url)`
- **Upsert lifecycle:** INSERT new jobs, UPDATE existing, SET `is_active=0` for disappeared jobs; empty batches are safe (no false deactivation)
- **Daily stats:** `company_daily_stats` table with active/new/closed/net_change per company per day
- **Momentum scoring:** `clamp(10*new + 2*net + log(active+1)*5, 0, 100)` — higher = more hiring activity
- **Query helpers:** `get_company_stats()`, `get_company_history()`, `get_summary_stats()` for API endpoints
- New DB tables: `jobs` (UNIQUE source+job_key), `company_daily_stats` (UNIQUE date+company+source)
- Idempotent migrations using `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`

### 9. `sync_ats_jobs.py` — ATS Job Sync Script (~80 lines)
- Pulls jobs from all ATS companies (Greenhouse, Lever, SmartRecruiters, Recruitee) using existing `normalize_jobs()` from `app.py`
- Upserts into `jobs` table via `job_intel.upsert_jobs()`
- 0.5s rate-limiting between companies
- Run: `python sync_ats_jobs.py` or `python sync_ats_jobs.py --dry-run`
- Run (single): `python sync_ats_jobs.py --company "Adyen"`

### 10. `daily_intelligence.py` — Daily Pipeline Orchestrator (~110 lines)
- Runs full pipeline in sequence: discovery -> scrape -> ATS sync -> stats
- Each step is independent (failures don't block subsequent steps)
- Stats computed directly via `job_intel.compute_daily_stats()` (no subprocess)
- Run: `python daily_intelligence.py` (full), `python daily_intelligence.py --stats-only`, `python daily_intelligence.py --skip-discover --skip-scrape`

### 11. `docker-compose.yml` — 4 services
- `api` — runs the FastAPI app
- `discover` — runs the static ATS prober
- `scrape` — runs the Playwright scraper
- `discover-ai` — runs the OSM discovery pipeline (no API key required unless `--use-ai-cleanup` is added)

---

## Architecture

```
                          osm_discover.py
                               |
                        (Overpass API, free)
                               |
                        candidate_filter.py
                               |
                        (score + filter)
                               |
discover.py --------+   agent_discover.py
                    |          |
detect.py ----------+--> companies.db --> app.py --> /ui, /ui/momentum
                               |               ^
                               |               |
                        agent_scrape.py --> scraped_jobs
                               |                         /stats/*
                               +---> job_intel.py --------^
                               |        |
                        sync_ats_jobs.py |
                                    jobs table + company_daily_stats
```

**Intelligence data flow:**
```
sync_ats_jobs.py --> normalize_jobs() --> upsert_jobs() --> jobs table
agent_scrape.py  --> save_jobs() hook --> upsert_jobs() --> jobs table
daily_intelligence.py --> compute_daily_stats() --> company_daily_stats
app.py /stats/* --> get_company_stats() / get_summary_stats() --> JSON
app.py /ui/momentum --> get_company_stats() --> HTML table (top 20)
```

**Data flow for `agent_discover.py`:**
```
OSM Overpass API --> raw candidates (740 for Eindhoven)
   --> score + filter (candidate_filter.py)  --> ~21 eligible
   --> [optional] AI cleanup (Claude Haiku)
   --> probe ATS endpoints + verify board name
   --> fallback: detect career page + verify domain
   --> store in companies.db
```

---

## Why We Removed Claude Sonnet Discovery

The original `agent_discover.py` used a Claude Sonnet agentic loop with DuckDuckGo web search to find companies. This was replaced in Session 2 for cost reasons:

| | Claude Sonnet + DuckDuckGo (v1) | OSM Pipeline (v2) |
|---|---|---|
| Cost per run | ~$3.33 | **$0.00** |
| Cost per company | ~$0.37 | **$0.00** |
| Companies per run | ~9 (limited by credits) | **21+ eligible** (unlimited) |
| API key required | Yes (always) | No (optional for AI cleanup) |
| Resumable | No | Yes (discovery_candidates table) |
| False positive rate | Low (AI evaluated) | Low (filter + ATS verification) |

**Claude Haiku is still available** as an optional cleanup step (`--use-ai-cleanup`). It costs ~$0.03 per run and normalises names, infers missing websites, and flags non-companies. It runs *after* the deterministic filter, so the batch is small and cheap.

DuckDuckGo search is no longer used anywhere in the codebase.

---

## Current Numbers (as of Mar 2, 2026)

| Metric                          | Value  |
|---------------------------------|--------|
| Total companies in DB           | 606    |
| ATS companies                   | 287    |
| Career page companies           | 319    |
| Scraped jobs in DB              | 1,105  |
| **Active jobs in intel table**  | **10,998** |
| **Companies with active jobs**  | **372**    |
| **Company daily stats rows**    | 2,138  |

---

## Session History

### Session 17: INCIDENT — Production Overload + Fixes (Jun 11, 2026, evening)

#### What happened (causal chain)
1. Morning: `description` column added; evening cron filled ~13.8k descriptions on prod
2. `aggregate_jobs` used `SELECT *` -> every search query started dragging ~19MB from SQLite
3. Sitemap (13.8k URLs) submitted to Google -> Googlebot began crawling, incl. paginated/filtered /jobs variants
4. Each uncached crawl hit = one 19MB full-table query on Railway's small container -> request pile-up -> service wedged (even /version stopped responding)
5. Frontend fetch timeouts (added same day) surfaced it as "Job search is briefly unavailable" on every page

#### Fixes (in this session, pending deploy)
- **Slim list query**: explicit column list in `aggregate_jobs` -- description/raw_json never fetched for search results (~19MB -> ~2MB per cold query)
- **10-min in-memory response cache** (`_AGG_CACHE`, max 32 filter combos, thread-safe, evicts oldest; cleared after bundle import + cron runs). Warm requests: 1.15s -> 0.008s. 50-page crawl burst: 0.5s total.
- **robots.ts**: `Disallow: /jobs?` -- crawlers skip filtered/paginated search variants (thin content anyway); all 13.8k detail pages remain in sitemap

#### Recovery runbook
1. Railway dashboard -> service -> check Deployments (did the last deploy fail?) + Metrics (memory/CPU ceiling?) -> **Restart** the service
2. Push backend fixes (app.py + job_alerts.py), confirm deploy goes green
3. Verify: `curl -o NUL -s -w "%{time_total}s" .../jobs?per_page=1` -- expect < 2s cold, then repeat -- expect < 0.5s warm
4. Push frontend robots.ts change
5. cubea.nl/jobs recovers within 10 min (Vercel cache window)
6. If Railway memory was at ceiling: consider bumping service resources or `--workers 2` in railway.toml start command (only if needed)

### Session 16: Phase 4 Milestone 1 — Job Alerts UI + Saved Jobs + Privacy (Jun 11, 2026)

#### Discovery: alert backend already existed
- `job_alerts.py` already had double opt-in, confirm/unsubscribe tokens, filter matching, digest HTML, SMTP + Resend support, and the daily cron sends digests. Phase 4 M1 = exposing it on the new frontend.

#### Backend (project/)
- `job_alerts.py`: `hidden` filter support in `match_jobs_for_alert()` (hidden_tier >= 1) + "Hidden gems only" in digest filter summary. Enables hidden-gems-only alert subscriptions.

#### Frontend (hireassit-cubeA/)
- `src/app/api/job-alert/route.ts` — server-side proxy to Railway `/api/alerts` (no CORS needed)
- `AlertSignup.tsx` on /jobs — captures active search filters (q/city/hidden/english) into the subscription; double-opt-in note + privacy link
- Saved jobs: `lib/savedJobs.ts` (localStorage, no account), `SaveButton` on job detail pages, `/jobs/saved` page, "★ Saved jobs" link on /jobs
- `/privacy` page — what's collected (email+filters only), deletion via success@cubea.nl, localStorage disclosure

#### Verified in sandbox (local API + next build + next start)
- Alert subscribe 200 ("Check your email"), invalid email 400, hidden-gem matcher returns matches, save button + saved page + privacy all render

#### Deploy notes
- **Railway must have email env vars set** for confirmations/digests to actually send: either `RESEND_API_KEY` or `SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/SMTP_FROM` (values in local .env). Test after deploy: `/api/alerts/smtp-test`
- Cron sends digests daily (skip_alerts=False by default in scheduled runs)

### Session 15: Phase 3 Milestone 1 — Next.js Job Search + SEO Pages (Jun 11, 2026)

#### Backend (project/)
- `jobs.description` column (idempotent migration); captured at sync, capped 1,500 chars plain text. Sources: Greenhouse `content` (entity-unescaped), Lever `description`, Recruitee `description`, Ashby `descriptionHtml`, HomeRun Atom `summary`. SmartRecruiters + careers_page have none (boilerplate fallback in frontend JSON-LD). Existing rows fill on next sync.
- `GET /jobs/{id}` — full job detail incl. description, hidden_tier, related jobs (same company, max 5). Inactive jobs return 200 + is_active=0 (frontend shows "no longer listed").
- `GET /meta/sitemap-jobs` — {id, slug, lastmod} for all active jobs.
- `/jobs` list items now include `id` + `slug` (`{title}-at-{company}-{id}`, lookup by trailing id).
- Bundle export/import carries `description`.

#### Frontend (hireassit-cubeA/, deploys to Vercel)
- `src/lib/hireassist.ts` — typed API client (HIREASSIST_API_URL env, defaults to Railway prod; ISR revalidate 10min list / 1h detail / 24h sitemap)
- `/jobs` — SSR search page: keyword + city + Hidden gems / English / New today pills, pagination, gradient hero with live count
- `/jobs/[slug]` — SEO detail page: generateMetadata, **JobPosting JSON-LD (Google Jobs)**, hidden-gem badges, description, Apply CTA (direct, nofollow), related jobs, noindex+gone-state for inactive
- `app/sitemap.ts` — daily-regenerated sitemap (~13.8k job URLs), `app/robots.ts`
- Nav: "HireAssist Alpha" -> "Jobs" (/jobs) in Landing + RecruiterLanding; `/tools/hireassist-alpha` 308-redirects to `/jobs`

#### Verified end-to-end in sandbox
- Local uvicorn (test DB) + `next build` + `next start`: search 200 (25 cards), detail 200 with JSON-LD + apply button, hidden filter, sitemap 13,802 URLs, alpha redirect 308, bad slug 404

#### Deploy notes
- Frontend fetches are server-side -> no Railway CORS change needed
- Vercel env (optional): `NEXT_PUBLIC_SITE_URL=https://cubea.nl`; `HIREASSIST_API_URL` only to override the baked-in Railway URL
- After deploy: submit https://cubea.nl/sitemap.xml in Google Search Console
- Descriptions populate after the next daily cron (or manual /admin/run-cron)

### Session 14: Phase 2 Start — Ashby + HomeRun ATS Support (Jun 11, 2026)

#### Two New Job Sources (now 6 ATS platforms + career pages)
- **Ashby** (`source="ashby"`): public posting API `https://api.ashbyhq.com/posting-api/job-board/{board}` — no auth. Token = board slug. Unlisted jobs filtered. Structured address used for city/country; `FullTime` -> `Full Time`.
- **HomeRun** (`source="homerun"`, Dutch ATS — prime hidden-gem source): no public JSON API, but two public surfaces: Atom feed `https://feed.homerun.co/{slug}` (preferred) and `{slug}.homerun.co/sitemap.xml` + server-rendered job pages (fallback, capped at 40 pages, 0.3s delay). Defaults country to Netherlands. Counts as small-ATS for hidden scoring (tier 1 "Low visibility").

#### Integration Points Touched
- `app.py`: `ashby_list_jobs()`, `homerun_list_jobs()`, normalize branches, SOURCE_NAMES
- `job_intel.py`: `ashby` uses provider job IDs for dedupe; `homerun` falls back to sha1 key; `homerun` added to `_SMALL_ATS_SOURCES`
- `sync_ats_jobs.py`: both added to ATS_SOURCES (daily cron syncs them automatically)
- `agent_discover.py`: `probe_ashby` + `probe_homerun` in PROBERS (token verification: ashby self-verifying, homerun via career-page `<title>`)
- `ats_reverse_discover.py`: URL patterns for `jobs.ashbyhq.com/{board}` and `{slug}.homerun.co`
- `test_new_sources.py` (new): live smoke test against known public boards (read-only)

#### Verified (mocked end-to-end in sandbox; live network unavailable there)
- Normalization, foreign-country tagging, NL defaults, stable dedupe keys (re-upsert = 0 new), hidden-tier assignment

### Session 13: Phase 1 — Hidden Gems + NL Data Quality (Jun 11, 2026)

#### Hidden-Job Scoring (`job_intel.py`)
- New columns on `jobs`: `hidden_score` (0-100) + `hidden_tier` (0/1/2), idempotent migration + index
- `compute_hidden_score()`: career-page/web_search sources +60, Recruitee +25, small company (<10/<25/<50 active jobs) +30/+20/+10, big-poster blocklist caps at 20
- Tiers: score >= 60 -> 2 "Hidden gem", >= 35 -> 1 "Low visibility", else 0
- `recompute_hidden_tiers()` runs automatically at the end of `compute_daily_stats()` (so the daily pipeline keeps tiers fresh); provisional tier set on insert in `upsert_jobs()`
- Bundle export/import carries both columns (Railway gets tiers on next push)

#### Foreign-Country Detection (`app.py`)
- `_FOREIGN_COUNTRY_MARKERS` (~60 countries), `_FOREIGN_CITIES` (~100 cities), US-state-abbreviation regex
- `split_city_country()` now marks non-NL jobs with a proper country instead of None
- `soft_country_match()`: `country=all` sentinel returns everything; remote jobs with no detectable country stay in the NL view

#### API + UI
- `/jobs` now defaults to `country=Netherlands` (pass `country=all` for everything) and supports `hidden=true`
- `/ui?hidden=true`: "Hidden gems" quick-filter pill activated (replaces the grayed-out "Not on LinkedIn" placeholder)
- Job cards show "Hidden gem" (purple) / "Low visibility" (amber) badges; pagination + sort links preserve the hidden param

#### Backfill (`backfill_phase1.py`, new)
- Re-parses location_raw to fill missing city/country (never overwrites non-empty values), recomputes tiers, prints before/after
- Verified against the Jun 2 bundle (sandbox test DB): 643 cities + 6,271 countries filled; foreign jobs 782 -> 6,595 detected; tiers: 2,317 gems / 1,271 low-visibility; default /jobs view 4,451 NL jobs, 0 foreign cities
- **Run once locally: `python backfill_phase1.py`** (or let the next daily pipeline converge over time)

#### Cleanup
- Removed `test_fresh.db` and the stray `c:Usersamita...tmp_jobs.json` file

#### Note
- `companies.db` could not be read through the Cowork file mount during this session ("database disk image is malformed" via the mount). Verify locally with `python -c "import sqlite3; print(sqlite3.connect('companies.db').execute('PRAGMA integrity_check').fetchone())"` — if it prints `('ok',)` the file is fine and it was a sync artifact. A copy from before any writes is at `companies.db.bak-phase1`.

### Session 12: Railway Migration + Career Page Fallback Fix (Feb 23, 2026)

#### Render Cold Start Optimization (`app.py`)
- **Background bundle import:** Moved the 4.4 MB bundle import from blocking `init_db()` to a background thread -- app accepts requests immediately on cold start
- **Batch SQL:** Converted all row-by-row `execute()` to `executemany()` in `_import_bundle_data()`
- **`/ping` endpoint:** Lightweight keep-alive returning `{"status": "ok", "ready": true/false}`
- **APScheduler self-ping:** Hits own public URL every 5 minutes to prevent cold starts; reads `RENDER_URL` or `RAILWAY_PUBLIC_DOMAIN`
- **`logging.basicConfig`:** Added so `logger.info` actually outputs to stdout (was silently dropped before)

#### Railway Migration
- **`railway.toml`:** Created with `uvicorn app:app --host 0.0.0.0 --port $PORT`, healthcheck on `/ping`
- **Cloud detection:** `init_db()` now checks both `RENDER=true` and `RAILWAY_ENVIRONMENT` for auto-importing bundles
- **`.env` updated:** `RENDER_URL` now points to `https://hireassist-backend-production.up.railway.app`
- **`ADMIN_TOKEN`** set as Railway service variable
- **cubea.nl site:** Updated `HireAssistAlpha.tsx` to use `/ping` instead of `/health` for status check; URL configured via `NEXT_PUBLIC_HIREASSIST_UI_URL` env var on Vercel/Netlify

#### Career Page Fallback Fix (Bug Fix in `agent_discover.py`)
- **The bug:** When ATS verification failed, `return "ats_mismatch"` on line 916 exited before reaching the career page fallback code (lines 918-940) that was already written
- **The fix:** Removed the early return so ATS rejection falls through to career page detection
- **Impact:** Companies without a supported ATS now get checked for career pages instead of being silently discarded

#### Discovery Runs
- **Google Places Eindhoven** (50 candidates) -- 36 new companies added
- **Re-processed 20 ATS-rejected Eindhoven candidates** -- 14 added (12 ATS, 2 career page); ABO-Milieuconsult and A. Leering Enschede recovered via career page fallback
- **Manual research additions:** Air Liquide (Workday, 19 jobs), Beckhoff Automation (2 jobs), Techwave Consulting (Workday, 2 jobs), Frencken (0 jobs currently)
- 59 Eindhoven ATS-rejected candidates remain to be re-processed

#### Key Commits
| Hash | Description |
|------|-------------|
| 9cda709 | Background bundle import, batch SQL, /ping endpoint |
| 6b21636 | APScheduler self-ping |
| ba41586 | railway.toml |
| bb3c094 | Cloud platform detection (Railway + Render) |
| 23a43fc | logging.basicConfig |
| d7f1cc1 | Career page fallback fix |

#### Stats Comparison
| Metric | Start of Session | End of Session |
|--------|-----------------|----------------|
| Companies | 487 | 593 |
| Jobs | ~6,285 | 7,622 |
| Scraped jobs | 592 | 600 |
| Career page companies | 51 | 55 |

### Session 11: Render Cold Start Optimization (Feb 22, 2026)

#### Background Bundle Import (`app.py`)
- Moved the Render startup bundle import (4.4 MB JSON, ~6k+ rows) from blocking `init_db()` to a background thread
- App now accepts HTTP requests immediately on cold start; data loads in parallel
- Added `_bundle_ready` threading.Event so endpoints can report readiness

#### Batch SQL Operations (`app.py`)
- Converted all row-by-row `conn.execute()` loops in `_import_bundle_data()` to `conn.executemany()` batch operations
- Applies to: companies, scraped_jobs, jobs, company_daily_stats tables
- Scraped jobs delete-per-company also batched (collect all company names first, then delete, then bulk insert)

#### Lightweight Keep-Alive Endpoint (`/ping`)
- Added `GET /ping` -- returns `{"status": "ok", "ready": true/false}` with zero DB or ATS calls
- Designed for uptime monitors (e.g. UptimeRobot free tier) to ping every ~10 minutes and prevent Render free-tier cold starts
- `ready` field reflects whether background bundle import has completed

### Session 10: Mobile Responsive + Pipeline Hardening (Feb 22, 2026)

#### Mobile Responsive UI (`app.py`)
- Added full mobile responsive design at 768px and 400px breakpoints
- **Hamburger menu:** 3-line SVG button replaces nav links on mobile; opens full-screen overlay with all nav items + Post a Job button
- **Stacked search bar:** Keyword, city, and search button stack vertically with individual borders on mobile
- **2-column stats grid:** Stats strip switches from horizontal row to 2x2 grid
- **Horizontal scroll quick filters:** Filter pills become non-wrapping horizontal scroll strip with hidden scrollbar
- **Single-column layout:** Sidebar and job list collapse to single column; sidebar hidden by default
- **Mobile filter toggle:** "Filters & Company Momentum" button toggles sidebar visibility via `.mobile-open` class
- **Job card adjustments:** Date label moves inline, footer stacks vertically, Apply button stretches full width

#### Google Places Discovery Fallback (`daily_intelligence.py`)
- Pipeline Step 1 now counts companies before/after OSM discovery run
- If OSM adds 0 companies (e.g. Overpass API overloaded with 504s), automatically falls back to Google Places discovery
- Runs: `agent_discover.py --source google --region <region> --limit 200`

#### Pipeline Automation Verified
- Confirmed Windows Task Scheduler task "HireAssist Daily Pipeline" is active, runs daily at 07:00
- Ran manual pipeline test -- all 7 steps completed successfully in ~26 minutes:
  - Discovery: OSM returned 0 (Overpass API overloaded), no Google fallback yet (added after)
  - Scrape: 51 career pages attempted, 36 returned jobs (592 total scraped)
  - ATS Sync: 434 companies synced, 22 new companies found via ATS reverse discovery
  - Stats: 204 company daily stats rows computed
  - Export: Bundle exported (7,540 jobs)
  - Push to Render: Successful
  - Git push: Bundle committed and pushed

#### Data Push
- Exported and pushed 487 companies + 7,540 jobs to Render production
- Bundle also committed to GitHub for Render cold start resilience

### Session 9: UI Redesign v2 (Feb 22, 2026)

#### Complete UI Overhaul (`app.py`)
- Replaced LinkedIn-inspired design with modern Inter font + blue gradient design system
- **Hero strip:** Blue gradient section with tagline ("Find jobs that nobody else shows you"), integrated search bar (keyword + city + hidden country), live stats strip showing jobs indexed / companies crawled / new today / cities covered
- **Quick filter pills:** Horizontal pill bar below hero for instant filtering (All, English only, New today, Eindhoven, Amsterdam); aspirational pills (Not on LinkedIn, Remote friendly) shown grayed out
- **Sidebar redesign:** Compact 2-column grid filter layout (search input full-width, company + city side-by-side, country below, checkboxes inline); auto-submit via `onchange` on all dropdowns and checkboxes -- no need to click Search for filter changes
- **Momentum widget:** Live top-5 company momentum sidebar with bar charts and delta scores, links to full `/ui/momentum` leaderboard
- **Job cards redesign:** Company initials logo (2-letter, blue circle), clickable title, meta row with SVG icons (location pin, briefcase), description snippet (2-line clamp), footer with type/source/new-today pills + Save + Apply buttons, time-ago labels from `updated_at`
- **Topbar navigation:** HireAssist logo + Jobs (active) / Companies / Company Momentum / For Employers / Post a Job
- **Alert banner:** "Get notified" banner with Set Job Alert button (visual-only, no backend)
- **Pagination:** Redesigned numbered pagination with arrows
- **`/ui/momentum` page:** Updated to match v2 design (Inter font, new topbar, new color scheme)
- **New helpers:** `time_ago()` converts ISO timestamps to "2 hours ago" etc; `company_initials()` extracts 2-letter logo text
- **Responsive:** Collapses to single column on < 800px viewport

#### Aspirational Features (visual-only, no backend)
- Save button (no-op), Job Alert banner (no-op), Post a Job link (#), For Employers link (#), Companies link (#)
- "Not on LinkedIn" and "Remote friendly" filter pills shown grayed out

#### Data Push
- Exported and pushed 416 companies + 6,285 jobs to Render production

### Session 5: Job Intelligence Layer (Feb 14, 2026)

#### New: Persistent Job Tracking (`job_intel.py`)
- Created `jobs` table: normalized index of ALL jobs (ATS + scraped) with lifecycle fields
  - `first_seen_at`, `last_seen_at`, `is_active` -- tracks when jobs appear and disappear
  - `UNIQUE(source, job_key)` -- stable deduplication across syncs
- Created `company_daily_stats` table: per-company daily snapshots (active/new/closed/net_change)
- **Dedupe key strategy:**
  - ATS sources (Greenhouse/Lever/SmartRecruiters/Recruitee): use provider's unique job ID
  - Scraped/careers_page: `sha1(company_lower|title_lower|normalized_url_netloc+path)`
- **Upsert lifecycle:** new jobs get `first_seen_at`; existing jobs get `last_seen_at` updated + re-activated; missing jobs get `is_active=0`. Empty batches are safe (no false closures).
- **Momentum scoring:** `clamp(10*new + 2*net_change + log(active+1)*5, 0, 100)`

#### New: ATS Sync Script (`sync_ats_jobs.py`)
- Imports `normalize_jobs()` from `app.py` (no code duplication)
- Loops all 69 ATS companies, upserts into `jobs` table
- Tested: Adyen (272 jobs) re-sync correctly shows 0 new + 272 updated on second run

#### New: Intelligence API Endpoints (in `app.py`)
- `GET /stats/companies?date=` -- per-company stats sorted by momentum
- `GET /stats/company/{name}?days=30` -- daily time series for one company
- `GET /stats/summary?days=7` -- aggregate totals
- `GET /ui/momentum` -- server-rendered HTML table of top 20 companies
- "Company Momentum" nav link added to `/ui` header
- All existing endpoints (`/ui`, `/jobs`, `/health`) unchanged

#### Modified: `agent_scrape.py` Intelligence Hook
- Added guarded import of `job_intel` (degrades gracefully if module missing)
- After `save_jobs()` commits scraped jobs, also upserts into `jobs` table
- ~15 lines added, wrapped in try/except -- zero risk to existing scrape pipeline

#### New: Daily Pipeline Orchestrator (`daily_intelligence.py`)
- Runs: discovery -> scrape -> ATS sync -> stats computation
- Flags: `--skip-discover`, `--skip-scrape`, `--stats-only`, `--region`
- Designed for Windows Task Scheduler / cron scheduling

#### DB Changes (safe, idempotent)
- New table: `jobs` (id, source, company_name, job_key, title, location_raw, country, city, url, posted_at, first_seen_at, last_seen_at, is_active, raw_json)
- New table: `company_daily_stats` (stat_date, company_name, source, active_jobs, new_jobs, closed_jobs, net_change)
- New indexes: `idx_jobs_company_active`, `idx_jobs_source_active`, `idx_jobs_first_seen`, `idx_stats_date`
- All created via `CREATE TABLE/INDEX IF NOT EXISTS` -- safe to run repeatedly

### Session 4: Scraper Upgrade — Workday, Portal Detection, Safe Replacement (Feb 14, 2026)

#### Workday Support (High Priority)
- Added `scrape_workday_jobs()` helper that uses Playwright `page.on("response")` to intercept `/wday/cxs/` JSON API responses
- `_extract_workday_json()` parses Workday's nested JSON structures (`jobPostings`, `listItems`, `bulletFields`)
- Detects Workday in 3 ways: URL pattern (`myworkdayjobs.com`), HTML markers (`wd-Application`, `jobPostingInfo`), or embedded links (e.g. WordPress page linking to Workday)
- `_find_workday_url()` extracts Workday portal links from non-Workday career pages (Vanderlande's WordPress site -> `vanderlande.wd3.myworkdayjobs.com`)
- Handles pagination via Workday's "Show More" / `loadMoreButton`
- **Result:** Vanderlande (19 jobs), Marel Poultry (19 jobs), Nexperia (18 jobs via Workday) now working

#### Global Portal Detection (Medium Priority)
- Added `_ATS_PORTAL_DOMAINS` map covering 8 ATS platforms: Greenhouse, Lever, SmartRecruiters, Workday, iCIMS, Taleo, Recruitee, Ashby
- `_detect_portal_redirect()` checks if the final URL after page load belongs to a known ATS domain
- `_scrape_portal_by_type()` dispatches to the appropriate scraper (Workday -> network interception, others -> HTML parse)
- Portal type is tracked and displayed in output (e.g. `[OK] 19 jobs [workday]`)

#### Enhanced JSON-LD Parsing
- `_parse_jsonld()` now parses ALL `<script type="application/ld+json">` blocks (not just first match)
- Flattens `@graph` arrays (common in WordPress/Yoast structured data)
- Handles `@type` as both string and list (`["JobPosting", "Thing"]`)
- Reads `name` field as fallback for `title`
- `_normalize_jsonld_location()` handles: multiple `jobLocation` entries, nested `addressRegion`, dict-format `addressCountry` (`{"name": "Netherlands"}`), string-format addresses

#### Safe Per-Company Job Replacement
- `save_jobs()` now deletes by `company_name` (not career URL), so URL changes don't leave orphaned rows
- Only deletes when fresh jobs are found (>= 1); preserves existing data on 0-result scrape
- On scrape failure (exception), existing jobs are left untouched
- Logging: "Replaced N old jobs with M fresh jobs for X" / "No jobs found, kept N existing" / "Scrape failed, preserved N old jobs"

#### Debug Mode (`--debug-company`)
- `--debug-company "Name"` scrapes a single company with verbose output
- Prints: final URL, detected portal type, API endpoints intercepted, job count, first 5 job titles with locations
- Useful for diagnosing why a specific company returns 0 jobs

#### End-of-Run Metrics
- Prints summary at end of every run:
  - Total companies attempted
  - Success with jobs / Success with 0 jobs / Failed (errors)
  - Total jobs inserted

#### Performance & Safety
- `context.set_default_timeout(30000)` as global timeout guard
- `scrape_career_page()` now returns 3-tuple: `(jobs, final_url, portal_type)`
- No per-company wait time increases; headless mode maintained
- No global delete behavior; no regressions in existing scraping

#### Before vs After (dry-run comparison)
| Metric               | Before (Session 3) | After (Session 4) | Change |
|----------------------|--------------------|--------------------|--------|
| Companies with jobs  | 24                 | 26                 | +2     |
| Total jobs scraped   | 440                | 488                | +48    |
| Failed (errors)      | varies             | 0                  | clean  |
| Remaining 0-job      | 28                 | 24                 | -4     |

#### Newly Working Companies
| Company          | Jobs | Method                                      |
|------------------|------|---------------------------------------------|
| Vanderlande      | 19   | Workday via embedded link (WordPress -> WD)  |
| Marel Poultry    | 19   | Workday via embedded link (direct)           |
| Nexperia         | 18   | Workday network interception                 |

### Session 3: Precision Improvements (Feb 14, 2026)

#### Candidate Scoring and Filtering
- Created **`candidate_filter.py`** with `score_candidate()` and `is_candidate_eligible()`
- Tag-based exclusion: `amenity=restaurant/cafe/bar`, `shop=*`, `tourism=*`, `leisure=*`, `healthcare=*`, etc.
- Tag-based preference: `office=company/it`, `industrial=*`, corporate name indicators
- Name-based exclusion: Dutch + English block list (bakker, kapsalon, hotel, tandarts, etc.)
- Result: **740 -> 21 eligible** for Eindhoven (filtered out 54 non-companies from top 75)

#### ATS Verification (token collision fix)
- After a probe returns jobs, fetches the ATS board display name and compares to candidate
- `_fetch_board_name()` queries Greenhouse boards API, Lever postings, SmartRecruiters company endpoint
- Short tokens (< 5 chars) require exact domain/name match to prevent collisions like "ABC" -> ThoughtWorks
- Result: "ABC Security Systems" -> greenhouse/ABC correctly **rejected** (board name = "ThoughtWorks_new")

#### Career Page Domain Verification
- `find_careers_page_verified()` checks that the final URL stays on the same registrable domain
- Allows redirects to known ATS domains (greenhouse.io, lever.co, workday.com, etc.)
- Rejects suspicious redirects to unrelated domains

#### New DB Columns (safe migration)
- Added to `discovery_candidates`: `score`, `reject_reason`, `ats_verified`, `website_domain`
- Migration is safe: uses `ALTER TABLE ... ADD COLUMN` only if column missing

#### New CLI Flags
- `--min-score` (default 30) -- minimum candidate score to proceed
- `--require-website` -- skip candidates without an OSM website tag
- `--no-strict-ats-verify` -- disable board name verification
- `--skip-ats` -- test filter + career detection only

### Session 2: OSM Discovery Pipeline (Feb 14, 2026)

#### Replaced AI Discovery with FREE OSM Pipeline
- **Problem:** `agent_discover.py` used Claude Sonnet + DuckDuckGo (~$0.37/company, $3.33 for 9 companies)
- **Solution:** Replaced with OpenStreetMap Overpass API (completely free, unlimited)
- Created **`osm_discover.py`** -- queries Overpass API for offices/companies in any Dutch region
- Rewrote **`agent_discover.py`** as a deterministic pipeline (no Claude agent loop)
- New DB table: **`discovery_candidates`** tracks all candidates with statuses (new/processed/rejected)
- New flags: `--source osm`, `--limit`, `--daily-target`, `--use-ai-cleanup`
- Logging to `data/logs/discover_YYYYMMDD.log`
- Updated `docker-compose.yml` to use OSM pipeline
- **Cost: $0.00/run** (vs $3.33 previously). AI cleanup optional at ~$0.03/run.

### Session 1: SPA Scraper + Eindhoven Discovery

#### SPA Fix for agent_scrape.py
- Added `scrape_career_page()` multi-strategy pipeline (iframe detection, link following, button clicking, scrolling)
- Extended `_JOB_URL_RE` regex to match `/career-opportunities/`, `/opportunities/`, `/vacancies/`
- Added `_JOB_HREF_KEYWORDS` for sub-page navigation
- Added `_LOAD_MORE_SELECTORS` for button clicking
- Fixed data loss: no longer deletes scraped_jobs when re-scrape yields 0
- Fixed unicode characters for Windows console compatibility
- **Result:** Career page scraping went from 8 working companies (99 jobs) to 24 working companies (440 jobs)

#### Eindhoven Company Discovery (manual)
- Searched for Eindhoven/Brainport tech companies
- Probed ATS tokens and detected career pages
- Added 24 new companies including: Prodrive Technologies (51 jobs), DAF Trucks (18 jobs), KMWE (18 jobs), Nexperia (8 jobs), Anteryon (5 jobs), Sioux Technologies (4 jobs), Ebusco (3 jobs), HighTechXL (2 jobs), EFFECT Photonics (1 job)

#### Eindhoven AI Discovery Run (legacy, now replaced)
- Ran the Claude Sonnet agent focused on Eindhoven
- Found 9 additional companies: Settels Savenije, Demcon, Carbyon, Axelera AI, VDL ETG, Neways Electronics, Nobleo Technology, Marel Poultry, Benchmark Electronics
- Run stopped when API credits ran out ($3.33 spent)

---

## Remaining Activities / TODO

### High Priority
1. **Run OSM discovery for more Dutch cities** -- Run `python agent_discover.py --region Amsterdam` (free!) for Amsterdam, Rotterdam, Utrecht, The Hague, Delft, Leiden, Groningen. Each run finds dozens of candidates at zero cost.

2. **Improve scraper for remaining 24 failing career pages** -- These companies have 0 scraped jobs:
   - Likely empty (no current openings): Ultimaker, SnappCar, Lightyear, Otrium, SMART Photonics, Carbyon
   - Custom JS integrations (Greenhouse API via JS, not iframe): Guerrilla Games
   - Complex SPAs needing further work: Channable, CM.com, Trengo, Vinted, Aiven, Mambu, Templafy, Treatwell
   - Academic portals: TU Eindhoven
   - Global portals: Uber NL (403 on HEAD, needs geo-filter)
   - Already on ATS (should use API, not scraping): Prosus (Lever)
   - Other: Axelera AI, Demcon, Itility, Temper, VDL ETG, VDL Groep
   - **DONE (Session 4):** ~~Vanderlande (Workday)~~, ~~Marel Poultry (Workday)~~, ~~Nexperia (Workday)~~

3. ~~**Git init + .gitignore**~~ -- **DONE.** Project is version-controlled on GitHub.

### Medium Priority
4. **Add `requirements.txt`** -- Dependencies are currently only in docker-compose. Should have:
   ```
   fastapi
   uvicorn
   requests
   beautifulsoup4
   playwright
   python-dotenv
   anthropic        # optional, only for --use-ai-cleanup
   apscheduler      # for self-ping keep-alive
   ```

5. ~~**Schedule daily intelligence pipeline**~~ -- **DONE (Session 8).** Windows Task Scheduler runs daily at 07:00.

6. ~~**Run full ATS sync**~~ -- **DONE.** All ATS companies synced via daily pipeline.

6b. **Set `NEXT_PUBLIC_HIREASSIST_UI_URL`** on cubea.nl hosting platform (Vercel/Netlify) to `https://hireassist-backend-production.up.railway.app/ui`

6c. **Re-process remaining 59 Eindhoven ATS-rejected candidates** -- These were rejected before the career page fallback fix; many may now qualify.

6d. **Process 334 pending OSM candidates nationwide** -- Discovery candidates awaiting processing.

7. **Improve job deduplication in /ui** -- Some companies appear via both ATS API and career page scraping, potentially showing duplicate jobs in the UI. The `jobs` table already deduplicates, but `/ui` still reads from live ATS + `scraped_jobs`.

7. **Add location/city tagging to scraped jobs** -- Many scraped jobs have empty `location_raw` fields. Could parse from job title or page structure.

### Low Priority
8. **Clean up temp files** -- Remove `tmp_jobs.json`, `nul`, `cUsersamitaSourceJobsNLprojecttmp_jobs.json` from project root.

9. **Add README.md** -- Document setup, usage, and architecture for the project.

10. ~~**Persistent job history**~~ -- **DONE (Session 5).** The `jobs` table tracks `first_seen_at`, `last_seen_at`, `is_active` for all jobs. `company_daily_stats` stores daily snapshots.

---

## How to Run

```bash
# Start the web UI (always running)
python -m uvicorn app:app --host 0.0.0.0 --port 8000

# ---- Job Intelligence (new in Session 5) ----
# Full daily pipeline (discover + scrape + sync + stats)
python daily_intelligence.py

# Full pipeline, skip discovery
python daily_intelligence.py --skip-discover

# Sync ATS jobs only (populate jobs table)
python sync_ats_jobs.py

# Sync a single ATS company
python sync_ats_jobs.py --company "Adyen"

# Recompute daily stats only
python daily_intelligence.py --stats-only

# ---- Scraping ----
# Refresh scraped jobs (run periodically)
python agent_scrape.py

# Debug a single company (detailed output)
python agent_scrape.py --debug-company "Vanderlande"

# ---- FREE Company Discovery (OSM pipeline) ----
# Discover companies in Eindhoven (free, ~2 min)
python agent_discover.py --region Eindhoven

# Discover companies across Noord-Brabant province
python agent_discover.py --region "Noord-Brabant" --limit 300

# Discover companies across all of Netherlands
python agent_discover.py --region Netherlands --daily-target 100

# Dry-run: see what would happen without writing to DB
python agent_discover.py --region Amsterdam --dry-run

# Strict mode: require website, high score threshold
python agent_discover.py --region Eindhoven --min-score 40 --require-website

# Skip ATS probing (career page detection only, faster)
python agent_discover.py --region Eindhoven --skip-ats

# With optional AI cleanup (~$0.03, needs ANTHROPIC_API_KEY in .env)
python agent_discover.py --region Eindhoven --use-ai-cleanup

# Test OSM query standalone
python osm_discover.py Eindhoven --limit 50
python osm_discover.py "Noord-Brabant" --json

# ---- Other tools ----
# Probe hardcoded company list for ATS tokens
python discover.py

# Detect career page URLs
python detect.py --add
```

## Key Files
- `companies.db` -- SQLite database (companies + scraped_jobs + discovery_candidates + jobs + company_daily_stats)
- `.env` -- Contains `ANTHROPIC_API_KEY` (optional, only needed for `--use-ai-cleanup`)
- `app.py` -- Main web application + intelligence API endpoints + momentum page
- `job_intel.py` -- Job intelligence layer (schema, upsert, stats, momentum scoring)
- `sync_ats_jobs.py` -- ATS job sync script (populates jobs table from ATS APIs)
- `daily_intelligence.py` -- Daily pipeline orchestrator (discover + scrape + sync + stats)
- `agent_scrape.py` -- Career page scraper with SPA support, Workday interception, portal detection, intel hook
- `agent_discover.py` -- Company discovery pipeline (OSM-based, free)
- `osm_discover.py` -- OpenStreetMap Overpass API module
- `candidate_filter.py` -- Candidate scoring and filtering logic
- `discover.py` -- Static ATS token prober
- `detect.py` -- Career page URL finder
- `data/logs/` -- Discovery run logs (one file per day)
