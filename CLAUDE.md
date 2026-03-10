# Business Analyst agent — CLAUDE.md

> Full PRD: `docs/prd.md` — read it for complete context. This file is the authoritative build guide.

## Project Summary

Web-based deal sourcing tool for filtering Swedish company lists (up to 40,000 rows from Allabolag.se exports) down to 10–30 acquisition candidates. Two-phase pipeline: Phase 1 filters on uploaded data only, Phase 2 scrapes historical financials + generates AI descriptions. Output is a Kanban pipeline with persistent state.

---

## Tech Stack (strict — do not substitute)

- **Backend:** Python 3.11+, FastAPI
- **Frontend:** HTML/CSS/vanilla JavaScript served by FastAPI (NO React, NO separate frontend framework)
- **Database:** PostgreSQL (production) / SQLite (local dev, auto-detected when DATABASE_URL is not set)
- **Data processing:** pandas, openpyxl, httpx (async), BeautifulSoup4
- **AI:** Anthropic Claude Haiku API — model: `claude-haiku-4-5-20251001`
- **Auth:** Single shared team password, bcrypt-hashed, JWT session tokens (30-day expiry)
- **Deployment:** Docker container (Hetzner VPS or Railway.app)
- **Export:** openpyxl for .xlsx output

---

## Project Structure

```
mispricing-hunter/
├── CLAUDE.md                  # This file
├── docs/
│   └── prd.md                 # Full PRD
├── main.py                    # FastAPI app entry point
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example               # Template for env vars
├── app/
│   ├── __init__.py
│   ├── config.py              # Settings, env var loading
│   ├── database.py            # DB engine, session, auto SQLite/PostgreSQL
│   ├── models.py              # SQLAlchemy models (all 7 tables)
│   ├── auth.py                # Password check, JWT issue/verify, display name cookie
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── dashboard.py       # GET /
│   │   ├── upload.py          # GET/POST /upload
│   │   ├── filter.py          # GET/POST /filter
│   │   ├── results.py         # GET /results
│   │   ├── enrich.py          # GET/POST /enrich (Phase 2 control)
│   │   ├── pipeline.py        # GET/POST /pipeline (Kanban)
│   │   ├── company.py         # GET/POST /company/{orgnr}
│   │   ├── export.py          # GET /export
│   │   └── settings.py        # GET/POST /settings
│   ├── services/
│   │   ├── __init__.py
│   │   ├── parser.py          # CSV/Excel parsing + hyperlink extraction
│   │   ├── phase1.py          # Hard + soft filtering logic
│   │   ├── phase2a.py         # Allabolag /bokslut scraping + historical filters
│   │   ├── phase2b.py         # Website fetch + Claude Haiku description
│   │   └── exporter.py        # Excel export with per-stage sheets
│   └── templates/             # Jinja2 HTML templates
│       ├── base.html
│       ├── login.html
│       ├── dashboard.html
│       ├── upload.html
│       ├── filter.html
│       ├── results.html
│       ├── enrich.html
│       ├── pipeline.html
│       ├── company.html
│       ├── export.html
│       └── settings.html
├── static/
│   ├── css/
│   ├── js/
│   └── img/
└── tests/
```

---

## Environment Variables

```
ANTHROPIC_API_KEY=             # Claude Haiku API key (Phase 2b). Never expose to frontend.
TEAM_PASSWORD_HASH=            # bcrypt hash of shared team password (min 16 chars)
JWT_SECRET=                    # Random 32-byte hex string
DATABASE_URL=                  # PostgreSQL connection string. Omit for SQLite auto-fallback.
PORT=8000                      # Server port
```

---

## Database Schema (7 tables)

All tables use ORG.NR (string, format "556036-0793") as the company identifier.

### companies
Master record per unique org number. Stores ALL Phase 1 columns from the Allabolag export, Phase 2 enrichment results (historical JSON), AI description text, current pipeline status (enum: unreviewed/watch/deep_dive/pass), allabolag_url, and timestamps. This is the single source of truth for company data.

### batches
One row per file upload. Fields: id, filename, upload_timestamp, filter_config_json, row_count_uploaded, row_count_phase1, row_count_phase2a, row_count_phase2b.

### batch_companies
Join table: batch_id + company_orgnr + phase1_passed (bool) + failed_filters (JSON array of filter names).

### pipeline_events
Append-only log. Fields: id, company_orgnr, from_status, to_status, user_name, timestamp. Never delete rows.

### notes
Append-only. Fields: id, company_orgnr, note_text, user_name, created_at. Never delete rows.

### filter_presets
Fields: id, name, config_json, created_by, created_at.

### phase2_jobs
Background job tracking. Fields: id, batch_id, status (enum: pending/running/paused/completed/failed), started_at, completed_at, companies_total, companies_done, last_completed_orgnr, errors_json.

**Cross-batch rule:** When a company (same org number) appears in a new batch but was previously marked "Pass", flag it as "Previously passed" in the UI but do NOT auto-exclude. Let the user decide.

---

## Input Data Specification

### File Formats
Accept both CSV and Excel (.xlsx). Auto-detect format. For Excel, allow sheet selection (default: first sheet named "Allabolag lista" if present).

### Critical: Hyperlink Extraction
The Allabolag URL for each company is an embedded hyperlink on the BOLAGSNAMN cell in the .xlsx file. You MUST extract these by reading the worksheet relationship XML inside the .xlsx ZIP archive at `xl/worksheets/_rels/sheet*.xml.rels`. Do NOT rely on openpyxl's hyperlink attribute alone — parse the rels XML directly.

URL format: `https://www.allabolag.se/foretag/{slug}/{city}/{category}/{orgnr-no-dash}/`

Fallback (if hyperlink missing, e.g. CSV): construct from org number → strip dash → `https://www.allabolag.se/{orgnr-no-dash}/bokslut`

### Column Names (Swedish — exact as they appear in export)
Primary columns used: BOLAGSNAMN, ORG.NR, BOLAGSTYP, REGISTRERINGSDATUM, ANTAL ANSTÄLLDA, SNI 1-5, SNI NAMN 1-5, OMSÄTTNING, ÅRETS RESULTAT, VINSTMARGINAL I %, SOLIDITET I %, HEMSIDA, ORDFÖRANDE, VERKSTÄLLANDE DIREKTÖR, ORT (BESÖK), LÄN, BOKSLUTSPERIOD SLUT, AKTIEKAPITAL, EGET KAPITAL, SUMMA TILLGÅNGAR, KASSA OCH BANK, LÖNER STYRELSE OCH VD

See `docs/prd.md` Appendix A for the full column reference with all 40+ columns.

### Deduplication
If the same ORG.NR appears multiple times in one upload, keep the row with the most recent BOKSLUTSPERIOD SLUT.

---

## Phase 1 — Hard Filtering

**No external HTTP requests.** All filtering runs on the uploaded data only.

### Filter Logic

Every filter is user-configurable in the UI with defaults below. Each filter has an enable/disable checkbox. Filter configs persist in the database per session.

**Hard-fail filters** (company excluded if it fails ANY enabled hard filter):

| Filter | Column | Default |
|---|---|---|
| Company type | BOLAGSTYP | Must contain "Aktiebolag" |
| Company age | REGISTRERINGSDATUM | ≥15 years from today |
| Revenue | OMSÄTTNING | 3,000,000 – 30,000,000 SEK |
| Employees | ANTAL ANSTÄLLDA | 3 – 30 |
| SNI code match | SNI 1–5 | Any of configured target codes (prefix match) |
| Profitability | ÅRETS RESULTAT | > 0 |

**Soft-fail filters** (contribute −1 penalty score each, do NOT exclude):

| Filter | Column | Default |
|---|---|---|
| Profit margin | VINSTMARGINAL I % | ≥10% |
| Soliditet | SOLIDITET I % | ≥50% |
| Data recency | BOKSLUTSPERIOD SLUT | Filed within last 18 months |
| County | LÄN | Optional multi-select |

### SNI Code Matching
Prefix-based: code "33" matches 33110, 33200, etc. User can edit the list in the UI. Default codes: 33, 43, 62, 71, 81, 25, 26, 27, 28, 46, 52, 69, 74, 78, 80, 85, 37, 38, 49

### Performance Requirement
Phase 1 on 40,000 rows MUST complete in under 30 seconds. Use pandas vectorized operations, not row-by-row iteration.

### Output Table
Sortable by any column, paginated (50 rows/page). Columns: Company name (linked to Allabolag), Org number, City/County, Industry (SNI), Age (years), Revenue (MSEK, 1 decimal), Employees, Net result (KSEK), Profit margin %, Soliditet %, Owner salary (KSEK), Cash (KSEK), Board chair/VD, Website (link), Soft flags count, Phase 2 status, Pipeline status (editable).

---

## Phase 2a — Historical Financial Scraping

Triggered manually via UI. User selects companies (default: all Phase 1 survivors). Job runs in background. Must support pause/resume. Show real-time progress bar + live log.

### Rate Limiting (CRITICAL)
Max 1 request per 0.5 seconds to Allabolag. Add random jitter ±0.2s. ~8–10 minutes for 1,000 companies. Communicate estimated time to user before starting.

### Scraping Target
URL: `{company_allabolag_url}/bokslut`

Parse the HTML table on the /bokslut page to extract per year: Räkenskapsår, Omsättning (KSEK), Årets resultat (KSEK), Antal anställda, Vinstmarginal %, Soliditet %

### Phase 2a Filters

**Hard-fail:**

| Filter | Default |
|---|---|
| Profitability 3/5 years | Positive net result in ≥3 of last 5 fiscal years |
| Revenue decline | Max decline ≤15% over any consecutive 2-year period in last 5 years |

**Soft-fail:**

| Filter | Default |
|---|---|
| Employee trend | Headcount not shrunk by >40% over last 5 years |
| Revenue CAGR | > -5% over 5 years |
| Consistent margin | Margin >5% in ≥3 of last 5 years |

### Error Handling
- < 3 years of data → flag "Insufficient history", skip profitability-3-of-5 filter (don't fail)
- Fetch error → retry up to 3 times with exponential backoff → then mark "Fetch failed" and continue
- Individual failures NEVER abort the entire job
- Log all errors to database: timestamp, orgnr, error message

### Job Recovery
If server restarts mid-job, resume from `last_completed_orgnr` in phase2_jobs table. Never restart from scratch.

---

## Phase 2b — AI Business Description

Runs AFTER Phase 2a completes. Only for companies that passed Phase 2a AND have a website listed.

### Process
1. HTTP GET the HEMSIDA URL. Follow ≤2 redirects. Timeout: 10 seconds.
2. Strip HTML/scripts/styles → extract visible text → truncate to 3,000 tokens.
3. Call Claude Haiku API:

**System prompt:**
```
You are an expert in Swedish B2B business analysis. You help an investor identify acquisition targets.
```

**User prompt:**
```
Based on the following website content from a Swedish company, write exactly 2 sentences in English: (1) what the company does and who its customers are, (2) any signals of competitive advantage, moat, or recurring revenue. Be specific and factual. Do not speculate. If the content is insufficient, say so.

Website content: {extracted_text}
```

4. Store the response in the companies table.
5. Also provide a "Generate description" / "Regenerate" button on the company detail page.

### API Key
Read from `ANTHROPIC_API_KEY` env var. NEVER expose in frontend or store in DB.

---

## Web Application Pages

### Authentication
Login screen → enter team password + optional display name. Display name stored in browser cookie for attribution. JWT with 30-day expiry.

### Routes (9 pages)

| Route | Function |
|---|---|
| `/` | Dashboard: pipeline summary stats, recent activity, quick links to Deep Dive companies |
| `/upload` | Drag-drop CSV/Excel, sheet selector, column preview, "Run Phase 1" button |
| `/filter` | All Phase 1 + Phase 2 filter thresholds, SNI code editor, save/load presets |
| `/results` | Phase 1 results table (sortable, filterable, paginated, pipeline status controls) |
| `/enrich` | Phase 2 control: select companies, start/pause, progress bar, live log, error log |
| `/pipeline` | Kanban board: 4 columns (Unreviewed/Watch/Deep Dive/Pass), drag-and-drop |
| `/company/{orgnr}` | Full company detail view |
| `/export` | Download pipeline as Excel (.xlsx) |
| `/settings` | Change password, manage filter presets, API usage stats |

### Kanban Board (`/pipeline`)
4 columns: Unreviewed (grey), Watch (blue), Deep Dive (green), Pass (red).

Card shows: company name (linked), city, revenue, employees, profit margin, soliditet, AI description (1 line), last note. Every card move logs: user_name + timestamp to pipeline_events.

### Company Detail Page (`/company/{orgnr}`)
- Header: name, orgnr, city, website link, Allabolag link, pipeline status dropdown
- Current financials grid: all key Phase 1 metrics
- Historical chart: line chart — revenue + net result over 5 years (after Phase 2a)
- Historical table: year-by-year breakdown
- AI description + "Regenerate" button
- Owner info: board chair + VD names (note: age must be checked manually on Ratsit.se)
- Notes: free-text input, timestamped, attributed, reverse-chronological
- Activity log: all pipeline status changes with user + timestamp

### Export
One .xlsx file with one sheet per pipeline stage (Unreviewed, Watch, Deep Dive, Pass). Each sheet has all company data columns + pipeline status + concatenated notes + last updated by + last updated at. Filename includes timestamp.

---

## Non-Functional Requirements

- Phase 1 filtering on 40,000 rows: < 30 seconds
- Phase 2 throughput: ≥ 100 companies/minute (after rate limit delays)
- Page load: < 2 seconds
- Phase 2 job recovery: resume from last completed company on restart
- Scraping errors: log and continue, never abort job
- Mobile-responsive: Kanban + company detail pages (upload/filter pages are desktop-only)
- Duplicate ORG.NR in upload: keep most recent BOKSLUTSPERIOD SLUT

---

## Out of Scope — DO NOT BUILD

- Automated owner age lookup (Ratsit)
- LinkedIn monitoring or outreach
- Bisnode / Creditsafe / UC Affärslogik integrations
- Email notifications
- Multi-tenancy / individual user accounts
- Financial modelling / valuation calculator
- Automated outreach emails
- Marketplace scraping (Flippa, Acquire.com)
- Any scraping beyond Allabolag /bokslut

---

## Build Order (suggested phased approach)

### Step 1: Foundation
- Project scaffolding, requirements.txt, Dockerfile, docker-compose.yml
- FastAPI app with config, database engine (SQLite/PostgreSQL auto-detect)
- All 7 SQLAlchemy models + Alembic migrations
- Auth system: login page, bcrypt password check, JWT, display name cookie
- Base HTML template with navigation

### Step 2: Upload + Parsing
- `/upload` page with drag-drop
- CSV + Excel parser with auto-detection
- Hyperlink extraction from .xlsx relationship XML
- Column mapping and validation
- Deduplication logic
- Batch creation in database

### Step 3: Phase 1 Filtering
- `/filter` page with all configurable thresholds + SNI code editor
- Filter preset save/load
- Phase 1 engine: vectorized pandas filtering
- `/results` page with sortable/paginated table
- Soft flag scoring

### Step 4: Phase 2a Scraping
- `/enrich` page with company selection + job controls
- Background job runner with pause/resume
- Allabolag /bokslut scraper with rate limiting + jitter
- HTML table parser for historical financials
- Phase 2a filter application
- Job recovery on restart
- Error logging

### Step 5: Phase 2b AI Descriptions
- Website fetcher with redirect following + timeout
- Text extraction from HTML
- Claude Haiku API integration
- "Regenerate" button on company detail

### Step 6: Pipeline + Kanban
- `/pipeline` Kanban board with drag-and-drop
- Pipeline event logging
- `/company/{orgnr}` detail page with all sections
- Notes system
- Historical financials chart (Chart.js or similar)

### Step 7: Export + Dashboard
- `/export` Excel export with per-stage sheets
- `/` Dashboard with summary stats + activity feed
- `/settings` page

### Step 8: Polish
- Mobile responsiveness for Kanban + company detail
- Error states and edge cases
- Docker + docker-compose verification
- End-to-end testing with sample data

---

## Code Style & Conventions

- Use async/await for all HTTP operations (httpx)
- Type hints on all function signatures
- Docstrings on all service functions
- Keep routers thin — business logic in services/
- Use Jinja2 templates for HTML (served by FastAPI)
- SQL via SQLAlchemy ORM, not raw queries
- All dates in ISO 8601 format
- Money values stored as integers (SEK öre) in the database, formatted for display
- Log all errors to both console and database
