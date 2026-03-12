# PRODUCT REQUIREMENTS DOCUMENT

**Business Analyst Agent**
**Swedish Business Acquisition Filtering System**

| Field   | Value                  |
|---------|------------------------|
| Version | v1.0                   |
| Status  | Ready for Development  |
| Scope   | Phase 1 + Phase 2     |

---

## 1. Overview & Purpose

The Business Analyst agent is a web-based deal sourcing tool that automates the filtering and enrichment of large Swedish company lists to surface acquisition candidates matching a specific investment thesis. The operator acquires undervalued Swedish businesses in the £10k–250k range, targeting stable B2B companies with aging owners, recurring revenue, and defensible niches. The tool eliminates the manual work of screening thousands of companies so the operator can spend time on the 10–30 highest-quality prospects each cycle.

### Investment Thesis (for context)

The ideal acquisition target is:

- A Swedish Aktiebolag (AB) that has been operating for 15+ years with consistent cash flows
- Owner-operated, with an aging owner (55+) who has no clear succession plan and may not have considered selling
- Operating in a boring B2B niche with sticky customers, recurring revenue, and regulatory or relationship-based moats
- Underpriced relative to its earnings power — often because the owner has not raised prices, has no sales function, and runs costs loosely
- Large enough to matter (3–30 MSEK revenue) but small enough that PE firms and aggregators ignore it

---

## 2. System Architecture

The system is a Python-based web application with a FastAPI backend, a lightweight frontend served by the same process, and a PostgreSQL database for persistent state. It is deployed to a cloud server (Hetzner or Railway) and accessed via a URL by all team members. Authentication uses a single shared team password.

### High-Level Pipeline

| Stage    | Name                  | Description |
|----------|-----------------------|-------------|
| Input    | Allabolag export      | CSV or Excel (.xlsx) file containing up to 40,000 company rows with embedded Allabolag hyperlinks in the BOLAGSNAMN column |
| Phase 1  | Hard Filtering        | Apply configurable current-year filters to reduce 40,000 → ~500–1,500 companies. Runs entirely on uploaded data, no external requests. |
| Phase 2a | Historical Enrichment | For each Phase 1 survivor, scrape the company's Allabolag /bokslut page to retrieve 5 years of historical financials and apply trend filters. Reduces to ~100–300. |
| Phase 2b | AI Description        | For each Phase 2a survivor, fetch the company website (from HEMSIDA column) and call Claude Haiku API to generate a 2-sentence business description. Estimated cost: <€0.10 per full run. |
| Output   | Pipeline Dashboard    | Ranked table + Kanban board (Watch / Deep Dive / Pass) with persistent state, user attribution, notes, and export to Excel. |

### Tech Stack

| Component        | Technology |
|------------------|------------|
| Backend          | Python 3.11+, FastAPI |
| Frontend         | HTML/CSS/JavaScript served by FastAPI (no separate framework required) |
| Database         | PostgreSQL (cloud) or SQLite (local dev) |
| Data processing  | pandas, openpyxl, httpx (async HTTP), BeautifulSoup4 |
| AI enrichment    | Anthropic Claude Haiku API (claude-haiku-4-5-20251001) |
| Authentication   | Single shared password, bcrypt-hashed, JWT session tokens |
| Deployment       | Docker container on Hetzner VPS or Railway.app |
| Export           | openpyxl for Excel output |

---

## 3. Input Data Specification

### 3.1 File Format

The system must accept both CSV and Excel (.xlsx) files exported from Allabolag.se. The upload interface must auto-detect the format. For Excel files, the user must be able to select which sheet to import (default: first sheet named "Allabolag lista" if present).

### 3.2 Allabolag Column Mapping

The following columns are present in a standard Allabolag premium export and are used by the system. All column names are in Swedish as they appear in the raw export.

| Column (Swedish) | Meaning | Notes |
|---|---|---|
| BOLAGSNAMN | Company name | Contains embedded hyperlink to the company's Allabolag page. The URL must be extracted from the Excel relationship XML at parse time. |
| ORG.NR | Organisation number | Used as the unique identifier for each company throughout the system (e.g. 556036-0793). |
| REGISTRERINGSDATUM | Registration date | Used to calculate company age in years. |
| ANTAL ANSTÄLLDA | Employee count | Current year headcount. |
| SNI 1–5 + SNI NAMN 1–5 | Industry codes | Up to 5 SNI codes per company. Filter matches on any of the 5. |
| OMSÄTTNING | Revenue (SEK) | Current fiscal year revenue. |
| ÅRETS RESULTAT | Net result (SEK) | Current year profit/loss. |
| VINSTMARGINAL I % | Profit margin % | Pre-calculated by Allabolag. |
| SOLIDITET I % | Equity ratio % | Pre-calculated by Allabolag. |
| HEMSIDA | Company website URL | Used in Phase 2b for AI description fetching. |
| BOLAGSTYP | Company type | Used to filter for Aktiebolag (AB) only. |
| ORDFÖRANDE / VD | Board chair / CEO names | Displayed in output for manual owner age lookup. |
| ORT (BESÖK) | City | Displayed in output. |
| LÄN | County | Displayed in output, available as filter. |
| BOKSLUTSPERIOD SLUT | Fiscal year end date | Used to determine how recent the financial data is. Warn if >18 months old. |
| AKTIEKAPITAL | Share capital (SEK) | Displayed in output. |
| EGET KAPITAL | Equity (SEK) | Displayed in output. |
| SUMMA TILLGÅNGAR | Total assets (SEK) | Displayed in output. |
| KASSA OCH BANK | Cash (SEK) | Displayed in output. |
| LÖNER STYRELSE OCH VD | Owner/board salary (SEK) | Important context: high owner salary = true profit higher than reported. |

### 3.3 Hyperlink Extraction

The Allabolag URL for each company is embedded as a hyperlink on the BOLAGSNAMN cell in the Excel file. The system must extract these URLs at parse time by reading the worksheet relationship XML inside the .xlsx ZIP archive (`xl/worksheets/_rels/sheet*.xml.rels`).

The URL format is:

```
https://www.allabolag.se/foretag/{slug}/{city}/{category}/{orgnr-no-dash}/
```

The `/bokslut` suffix is appended to this URL to retrieve historical financials in Phase 2a:

```
https://www.allabolag.se/foretag/{slug}/{city}/{category}/{orgnr}/bokslut
```

If the hyperlink is missing for a company row (e.g. CSV upload with no embedded links), the URL should be constructed from the org number as a fallback: strip the dash from ORG.NR and use `https://www.allabolag.se/{orgnr-no-dash}/bokslut`.

---

## 4. Phase 1 — Hard Filtering

### 4.1 Purpose

Phase 1 applies configurable hard filters to the uploaded company list using only the data present in the export file. No external HTTP requests are made. The goal is to reduce 40,000 rows to a manageable shortlist of ~500–1,500 companies that warrant historical enrichment in Phase 2.

### 4.2 Filter Configuration UI

All filter thresholds must be user-configurable via the web UI before running. Each filter has a default value (specified below) that can be overridden. Filters can be individually enabled or disabled via checkbox. The current filter configuration must be saved per user session and persist in the database so the same settings are pre-loaded on the next visit.

### 4.3 Phase 1 Filters

| Filter | Source Column | Default Threshold | Hard Fail? |
|---|---|---|---|
| Company type | BOLAGSTYP | Must contain "Aktiebolag" | Yes |
| Company age | REGISTRERINGSDATUM | ≥15 years before today | Yes |
| Revenue (MSEK) | OMSÄTTNING | 3,000,000 – 30,000,000 SEK | Yes |
| Employee count | ANTAL ANSTÄLLDA | 3 – 30 employees | Yes |
| SNI code match | SNI 1–5 | Any of configured target codes | Yes |
| Current profitability | ÅRETS RESULTAT | > 0 (positive result) | Yes |
| Profit margin | VINSTMARGINAL I % | ≥10% | No |
| Soliditet (equity ratio) | SOLIDITET I % | ≥50% | No |
| Data recency | BOKSLUTSPERIOD SLUT | Filed within last 18 months | No |
| County (Län) | LÄN | Optional multi-select | No |

Hard Fail = Yes means the company is excluded from all further processing if it fails this filter. Hard Fail = No means the filter contributes to a score but does not auto-exclude. Soft-fail filters each contribute −1 to a penalty score displayed alongside each company in the output.

### 4.4 SNI Code Configuration

The SNI code filter must support a free-text list of codes that the user can edit in the UI. Matching is prefix-based: entering "33" matches all codes starting with 33 (e.g. 33110, 33200). Multiple codes are separated by commas or newlines. The default list is pre-populated with the target codes from the investment thesis:

| SNI Prefix | Industry Description |
|---|---|
| 33 | Repair and installation of machinery and equipment |
| 43 | Specialised construction activities |
| 62 | Computer programming, consultancy |
| 71 | Architectural and engineering activities |
| 81 | Services to buildings and landscape activities |
| 25 | Manufacture of fabricated metal products |
| 26 | Manufacture of computer, electronic, optical products |
| 27 | Manufacture of electrical equipment |
| 28 | Manufacture of machinery and equipment n.e.c. |
| 46 | Wholesale trade (excl. motor vehicles) |
| 52 | Warehousing and support activities for transportation |
| 69 | Legal and accounting activities |
| 74 | Other professional, scientific and technical activities |
| 78 | Employment activities |
| 80 | Security and investigation activities |
| 85 | Education |
| 37 | Sewerage |
| 38 | Waste collection and treatment |
| 49 | Land transport and transport via pipelines |

### 4.5 Phase 1 Output

After filtering, the system displays a results table with the following columns. The table is sortable by any column and paginated (50 rows per page).

| Column | Description |
|---|---|
| Company name | Clickable link to Allabolag page (opens in new tab) |
| Org number | As formatted in export (e.g. 556036-0793) |
| City / County | From ORT (BESÖK) and LÄN |
| Industry (SNI) | First matched SNI code + name |
| Age (years) | Calculated from REGISTRERINGSDATUM |
| Revenue (MSEK) | OMSÄTTNING / 1,000,000, rounded to 1 decimal |
| Employees | ANTAL ANSTÄLLDA |
| Net result (KSEK) | ÅRETS RESULTAT / 1,000 |
| Profit margin % | VINSTMARGINAL I % |
| Soliditet % | SOLIDITET I % |
| Owner salary (KSEK) | LÖNER STYRELSE OCH VD / 1,000 (important context) |
| Cash (KSEK) | KASSA OCH BANK / 1,000 |
| Board chair / VD | ORDFÖRANDE and VERKSTÄLLANDE DIREKTÖR (for manual owner age lookup) |
| Website | HEMSIDA, clickable link |
| Soft flags | Count of soft-fail filters triggered (e.g. "2 flags" in orange) |
| Phase 2 status | Not started / Running / Complete / Failed |
| Pipeline status | Unreviewed / Watch / Deep Dive / Pass (editable) |

---

## 5. Phase 2 — Historical Enrichment & AI Description

### 5.1 Purpose

Phase 2 enriches the Phase 1 shortlist with data not available in the Allabolag export: (a) historical financials from the company's Allabolag /bokslut page, enabling trend-based filters; and (b) a plain-language business description generated by Claude Haiku from the company's own website.

### 5.2 Execution Model

Phase 2 runs as a background job triggered manually by the user from the UI. It does not run automatically after Phase 1. The user selects which companies to enrich (default: all Phase 1 survivors, or a filtered subset) and clicks "Run Phase 2". Progress is shown in real time via a progress bar and live log. The job can be paused and resumed.

**Rate Limiting:** Allabolag requests must be throttled to a maximum of 1 request per 0.5 seconds (120 requests/minute) with randomised jitter (±0.2s) to avoid triggering anti-scraping measures. For a shortlist of 1,000 companies, Phase 2a will take approximately 8–10 minutes. This is clearly communicated to the user before the job starts.

### 5.3 Phase 2a — Historical Financial Scraping

#### Data Source

Each company's Allabolag /bokslut page displays financial data across three distinct HTML sections. The scraper must extract every field listed below as displayed directly on the page for up to 5 fiscal years. Do NOT download PDF annual reports — scrape only the HTML content rendered on the page.

---

**Section 1: Bokslut — Bokslutsperiod**

| Field | Swedish label |
|---|---|
| Fiscal year start date | Startdatum |
| Fiscal year end date | Slutdatum |

---

**Section 2: Bokslut — Löner & Utdelning** (Belopp i 1000)

| Field | Swedish label |
|---|---|
| Currency code | Valutakod |
| Board and CEO salaries | Löner styrelse och VD |
| Other salaries | Löner övriga |
| Proposed dividend | Föreslagen utdelning |

---

**Section 3: Bokslut — Resultaträkning** (Belopp i 1000)

| Field | Swedish label |
|---|---|
| Currency code | Valutakod |
| Net revenue | Nettoomsättning |
| Other revenue | Övrig omsättning |
| Total revenue | Omsättning |
| Inventory change | Lagerförändring |
| Operating costs | Rörelsekostnader |
| Operating result after depreciation | Rörelseresultat efter avskrivningar |
| Financial income | Finansiella intäkter |
| Financial costs | Finansiella kostnader |
| Result after financial net | Resultat efter finansnetto |
| Result before tax | Resultat före skatt |
| Tax on year's result | Skatt på årets resultat |
| Net result | Årets resultat |

---

**Section 4: Bokslut — Balansräkning** (Belopp i 1000)

| Field | Swedish label |
|---|---|
| Currency code | Valutakod |
| Intangible fixed assets | Immateriella anläggningstillgångar |
| Tangible fixed assets | Materiella anläggningstillgångar |
| Financial fixed assets | Finansiella anläggningstillgångar |
| Total fixed assets | Anläggningstillgångar |
| Inventory | Varulager |
| Accounts receivable | Kundfordringar |
| Cash and bank | Kassa och bank |
| Total current assets | Omsättningstillgångar |
| Total assets | Summa tillgångar |
| Unrestricted equity | Fritt eget kapital |
| Untaxed reserves | Obeskattade reserver |
| Total equity | Eget kapital |
| Provisions | Avsättningar |
| Long-term liabilities | Långfristiga skulder |
| Accounts payable | Leverantörsskulder |
| Total short-term liabilities | Kortfristiga skulder |
| Total equity and liabilities | Summa eget kapital och skulder |

---

**Section 5: Nyckeltal** (Belopp i 1000)

| Field | Swedish label |
|---|---|
| Profit margin % | Vinstmarginal i % |
| Quick ratio % | Kassalikviditet i % |
| Equity ratio % | Soliditet i % |
| Debt-to-equity ratio | Skuldsättningsgrad |
| Return on equity % | Avkastning eget kapital i % |
| Return on total capital % | Avkastning totalt kapital i % |
| Employees | Anställda |
| Personnel cost per employee (KSEK) | Personalkostnader per anställd (i 1000) |
| EBITDA (KSEK) | EBITDA (i 1000) |

---

All fields must be stored per fiscal year in the database. If a field is absent from the HTML for a given year, store NULL — do not skip the row. The system must handle pages that show fewer than 5 years without error.

#### Phase 2a Filters

After extracting historical data, apply the following additional filters. These are also user-configurable. A company fails Phase 2a if it fails any enabled hard filter.

| Filter | Source Column | Default Threshold | Hard Fail? |
|---|---|---|---|
| Profitability 3 of 5 years | Årets resultat (historical) | Positive result in ≥3 of last 5 fiscal years | Yes |
| Revenue decline | Omsättning (historical) | Max decline ≤15% over any consecutive 2-year period in last 5 years | Yes |
| Employee trend | Antal anställda (historical) | Headcount has not shrunk by >40% over last 5 years | No |
| Revenue trend direction | Omsättning (historical) | Flat or growing over 5-year period (CAGR > -5%) | No |
| Consistent margin | Vinstmarginal % (historical) | Margin >5% in at least 3 of last 5 years | No |

#### Handling Missing Data

If the /bokslut page returns fewer than 3 years of data, the company is flagged as "Insufficient history" and the profitability-3-of-5 filter is skipped (not failed). The company remains in the pipeline with a warning flag. If the page cannot be fetched (network error, 404, rate-limited), the system retries up to 3 times with exponential backoff before marking the company as "Fetch failed" and moving on.

### 5.4 Phase 2b — AI Business Description

#### Trigger

Phase 2b runs after Phase 2a completes, only for companies that passed Phase 2a. It can also be triggered independently for any company via a "Generate description" button in the company detail view.

#### Process

1. Fetch the company website URL from the HEMSIDA column.
2. If no website is listed, skip Phase 2b and mark as "No website."
3. HTTP GET the website homepage. Follow up to 2 redirects. Timeout after 10 seconds.
4. Extract visible text content (strip HTML, scripts, styles). Truncate to 3,000 tokens.
5. Send to Claude Haiku (claude-haiku-4-5-20251001) with the following prompt:

**System prompt:**

```
You are an expert in Swedish B2B business analysis. You help an investor identify acquisition targets.
```

**User prompt:**

```
Based on the following website content from a Swedish company, write exactly 2 sentences in English: (1) what the company does and who its customers are, (2) any signals of competitive advantage, moat, or recurring revenue. Be specific and factual. Do not speculate. If the content is insufficient, say so.

Website content: {extracted_text}
```

#### Cost Estimate

| Item | Detail |
|---|---|
| Model | Claude Haiku (claude-haiku-4-5-20251001) |
| Input tokens per company | ~2,000 (website text + prompt) |
| Output tokens per company | ~150 (2-sentence description) |
| Cost per company | ~€0.0003 |
| Cost for 300 companies | ~€0.09 |
| Cost for 1,000 companies | ~€0.30 |

#### API Key Configuration

The Anthropic API key is stored as an environment variable (`ANTHROPIC_API_KEY`) in the server environment. It is never exposed in the frontend or stored in the database. The key is set once during deployment and does not need to be entered by users.

---

## 6. Web Application & User Interface

### 6.1 Authentication

The application is protected by a single shared team password. On first visit, users are presented with a login screen. They enter the team password and, optionally, their display name (e.g. "Hawaar", "Anna"). The display name is stored in a browser cookie and used for user attribution on pipeline actions. The password is bcrypt-hashed and stored in the server environment (not in the database). JWT tokens with a 30-day expiry are issued on successful login.

**Security Note:** The shared password model is appropriate for a small trusted team. The password should be at least 16 characters and shared securely (not via email). For future hardening, individual user accounts can be added without restructuring the application.

### 6.2 Application Pages

| Page / Route | Purpose |
|---|---|
| `/` (Dashboard) | Overview: pipeline summary stats, recent activity feed, quick-access to active Deep Dive companies |
| `/upload` | File upload page: drag-and-drop CSV/Excel, sheet selector, column preview, "Run Phase 1" button |
| `/filter` | Filter configuration: all Phase 1 and Phase 2 thresholds, SNI code list editor, save/load filter presets |
| `/results` | Phase 1 results table: sortable, filterable, paginated, with pipeline status controls |
| `/enrich` | Phase 2 control panel: select companies to enrich, start/pause job, live progress, error log |
| `/pipeline` | Kanban board: Watch / Deep Dive / Pass columns with drag-and-drop, notes, user attribution |
| `/company/{orgnr}` | Company detail view: all financial data, historical chart, AI description, notes history, activity log |
| `/export` | Export current pipeline to Excel, with all data and pipeline status included |
| `/settings` | Change team password, manage filter presets, view API usage stats |

### 6.3 Kanban Pipeline Board (`/pipeline`)

The Kanban board is the primary daily-use view. It has four columns:

| Column | Description | Meaning |
|---|---|---|
| Unreviewed | Default state for all Phase 1/2 survivors. Grey. | Companies awaiting first human review. |
| Watch | Interesting but not yet ready for deep dive. Blue. | Monitor for now. Could become a Deep Dive. |
| Deep Dive | Serious prospect. Active analysis ongoing. Green. | Due diligence has been initiated or is planned. |
| Pass | Rejected. Red. | Does not meet criteria. Optionally add a rejection reason. |

Each company card on the Kanban shows: company name (linked to detail page), city, revenue, employees, profit margin, soliditet, AI description (truncated to 1 line), and the last note added. Moving a card records the action with the user's display name and a timestamp.

### 6.4 Company Detail Page (`/company/{orgnr}`)

The company detail page aggregates all data for a single company. It includes:

- **Header:** company name, org number, city, website link, Allabolag link, pipeline status (editable dropdown)
- **Current financials panel:** all key metrics from the Phase 1 export in a clear grid
- **Historical financials chart:** line chart showing revenue and net result over 5 years (populated after Phase 2a)
- **Historical financials table:** year-by-year breakdown of all scraped metrics
- **AI description:** the Phase 2b output, with a "Regenerate" button
- **Owner info:** board chair and VD names (with note that owner age must be checked manually on Ratsit.se)
- **Notes:** free-text note input, timestamped and attributed to the user who wrote it, displayed in reverse-chronological order
- **Activity log:** all pipeline status changes with user + timestamp

### 6.5 Export

The export page allows downloading the current pipeline as an Excel file (.xlsx). The export includes one sheet per pipeline stage (Unreviewed, Watch, Deep Dive, Pass), each containing all company data columns plus: pipeline status, notes (concatenated), last updated by, last updated at. The export reflects the current filter run — each export is timestamped in the filename.

---

## 7. Data Persistence

### 7.1 Database Schema Overview

The application uses PostgreSQL in production and SQLite in local development. The schema must support multiple filter runs (batches) so that a new upload does not overwrite previous pipeline work. Companies are identified by org number across batches.

| Table | Purpose |
|---|---|
| companies | Master record per unique org number. Stores all Phase 1 data, Phase 2 enrichment results, AI description, and current pipeline status. |
| batches | Each file upload creates a batch record. Stores filename, upload timestamp, filter config used, and row counts at each stage. |
| batch_companies | Join table linking companies to batches with their Phase 1 pass/fail result and which filters they failed. |
| pipeline_events | Append-only log of every pipeline status change: company orgnr, from_status, to_status, user_name, timestamp. |
| notes | Append-only notes table: company orgnr, note_text, user_name, created_at. |
| filter_presets | Saved filter configurations: name, config JSON, created_by, created_at. |
| phase2_jobs | Background job tracking: batch_id, status, started_at, completed_at, companies_total, companies_done, errors. |

### 7.2 Cross-Batch Behaviour

When a company appears in multiple upload batches (same org number), its pipeline status, notes, and activity log are preserved across batches. If a company was previously marked as "Pass" and appears again in a new batch, it is flagged as "Previously passed" but not auto-excluded — the user can choose to keep it in the new batch or skip it. This prevents re-reviewing the same company repeatedly.

---

## 8. Deployment & Infrastructure

### 8.1 Deployment Target

The application is packaged as a Docker container and deployed to either Hetzner Cloud (CX21 instance, ~€5/month) or Railway.app (Starter plan, free tier or ~$5/month). Both options provide a public URL accessible from any browser. HTTPS is handled automatically by the platform (Let's Encrypt or Railway TLS).

### 8.2 Environment Variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude Haiku (Phase 2b). Set once at deployment. |
| `TEAM_PASSWORD_HASH` | bcrypt hash of the shared team password. |
| `JWT_SECRET` | Random 32-byte secret for signing JWT tokens. |
| `DATABASE_URL` | PostgreSQL connection string (or omit for SQLite in local dev). |
| `PORT` | Port to bind the server to (default: 8000). |

### 8.3 Local Development

The application must also run locally with a single command for development and testing purposes:

```bash
pip install -r requirements.txt && uvicorn main:app --reload
```

In local mode, SQLite is used automatically if `DATABASE_URL` is not set. The application opens at `http://localhost:8000`.

### 8.4 Docker

A `Dockerfile` and `docker-compose.yml` must be included in the repository. `docker-compose up` should start both the application and a PostgreSQL container with no additional configuration. This is used both for local development with Docker and for VPS deployment.

---

## 9. Non-Functional Requirements

| Category | Requirement | Detail |
|---|---|---|
| Performance | Phase 1 filtering of 40,000 rows | Must complete in under 30 seconds after file upload. |
| Performance | Phase 2 throughput | Must process at least 100 companies per minute (after rate limiting delay). |
| Performance | Page load time | All pages must load in under 2 seconds on a standard broadband connection. |
| Reliability | Phase 2 job recovery | If the server restarts mid-job, the job must resume from the last completed company, not restart from scratch. |
| Reliability | Scraping error handling | Individual company fetch failures must not abort the entire Phase 2 job. Log the error and continue. |
| Usability | Mobile-friendly | The Kanban and company detail views must be usable on a phone (responsive layout). The upload and filter config pages are desktop-only. |
| Data integrity | Duplicate detection | If the same org number appears multiple times in an upload file, deduplicate and keep the row with the most recent BOKSLUTSPERIOD SLUT. |
| Observability | Activity log | All Phase 2 scraping errors must be logged to the database with timestamp, company orgnr, and error message for post-run review. |

---

## 10. Out of Scope (Explicitly)

The following are deliberately excluded from this version and should not be built:

- Automated owner age lookup via Ratsit (owner age is checked manually using the provided board chair/VD names)
- Automated LinkedIn monitoring or outreach generation
- Integration with Bisnode, Creditsafe, or UC Affärslogik APIs
- Email notifications or alerts
- Multi-tenancy or organisation-level user management (single team password is sufficient)
- In-app financial modelling or valuation calculator
- Automated outreach email generation
- Physical asset marketplace scraping (Flippa, Acquire.com, etc.)
- Any scraping of Ratsit, Bolagsverket, or other Swedish registries beyond Allabolag /bokslut

---

## 11. Potential Future Phases

These are noted for context but must not influence the current build:

- **Phase 3:** Ratsit scraping for board member age, cross-referenced with company data to auto-score succession signals
- **Phase 4:** Automated outreach email generation using Claude, triggered from the Deep Dive pipeline stage
- **Phase 5:** Individual user accounts with role-based permissions (viewer vs. editor)
- **Phase 6:** Integration with calendar/CRM to track owner conversations and follow-ups
- **Phase 7:** Multi-source ingestion (Flippa, Acquire.com, Bolagsplatsen.se) unified into the same pipeline

---

## 12. Acceptance Criteria

The build is considered complete when all of the following are true:

1. A CSV or Excel file exported from Allabolag can be uploaded and fully parsed, including extraction of embedded hyperlinks from the BOLAGSNAMN column.
2. Phase 1 filtering runs on a 40,000-row file and completes within 30 seconds, producing a results table with all specified columns.
3. All Phase 1 filter thresholds are configurable in the UI, individually toggleable, and persist between sessions.
4. Phase 2a scrapes the /bokslut page for each shortlisted company, extracts 5 years of historical financials, and applies the historical trend filters.
5. Phase 2a respects the 0.5-second rate limit with jitter and handles fetch errors gracefully without aborting the job.
6. Phase 2b fetches company websites and generates a 2-sentence Claude Haiku description for each company that passes Phase 2a and has a listed website.
7. The Kanban board displays all pipeline stages, supports drag-and-drop status changes, and records user name and timestamp for every action.
8. The company detail page shows all financial data, the 5-year historical chart, the AI description, notes, and the full activity log.
9. Pipeline state, notes, and activity logs persist between server restarts and across multiple upload batches.
10. The application is accessible via a public HTTPS URL, protected by the shared team password, and usable simultaneously by multiple users without data conflicts.
11. An Excel export of the pipeline can be downloaded at any time, with one sheet per pipeline stage.
12. A previously-passed company reappearing in a new batch is flagged as "Previously passed" rather than silently re-entered.

---

## Appendix A — Allabolag Column Reference

Complete list of columns present in a standard Allabolag premium export, for reference during development. Columns marked with ✓ are actively used by the system.

| Column | Description | System Use |
|---|---|---|
| ✓ BOLAGSNAMN | Company name (with embedded hyperlink) | Primary identifier display |
| ✓ ORG.NR | Organisation number | Primary key |
| ✓ BOLAGSTYP | Company type | Filter: Aktiebolag only |
| MARKNADSNAMN | Trade name | Informational |
| ✓ ALLABOLAG-BRANSCH 1–8 | Allabolag category labels | Informational display |
| ✓ SNI 1–5 + SNI NAMN 1–5 | SNI codes and names | Core filter |
| ADRESS (BESÖK / POST) | Address fields | Informational |
| POSTNR, ORT, LÄN, KOMMUN | Postal and region data | Filter and display |
| TELEFON, MOBIL, FAX | Phone numbers | Display |
| ✓ ANTAL ANSTÄLLDA | Employee count | Core filter |
| ✓ ORDFÖRANDE | Board chair | Display (manual owner age lookup) |
| ✓ VERKSTÄLLANDE DIREKTÖR | CEO | Display (manual owner age lookup) |
| ✓ HEMSIDA | Website URL | Phase 2b input |
| E-POST | Email address | Display |
| ✓ REGISTRERINGSDATUM | Registration date | Company age filter |
| REGISTRERAD FÖR MOMS/F-SKATT | Tax registration status | Informational |
| ✓ AKTIEKAPITAL | Share capital | Display |
| BOKSLUTSPERIOD START/SLUT | Fiscal year dates | Data recency filter |
| VALUTAKOD | Currency code | Always SEK in scope |
| ✓ OMSÄTTNING | Revenue | Core filter |
| ✓ ÅRETS RESULTAT | Net result | Core filter |
| ✓ RESULTAT FÖRE SKATT | Pre-tax result | Display |
| ✓ VINSTMARGINAL I % | Profit margin | Soft filter |
| ✓ SOLIDITET I % | Equity ratio | Soft filter |
| ✓ EGET KAPITAL | Equity | Display |
| ✓ SUMMA TILLGÅNGAR | Total assets | Display |
| ✓ KASSA OCH BANK | Cash and bank | Display |
| ✓ LÖNER STYRELSE OCH VD | Owner/board salary | Important context display |
| LÖNER ÖVRIGA | Other staff salaries | Display |
| ✓ RÖRELSERESULTAT EFTER AVSKRIVNINGAR | EBIT | Display |
| KORTFRISTIGA SKULDER | Current liabilities | Display |
| LÅNGFRISTIGA SKULDER | Long-term debt | Display |
| LEVERANTÖRSSKULDER | Accounts payable | Display |
| KUNDFORDRINGAR | Accounts receivable | Display |
| KASSALIKVIDITET I % | Quick ratio | Display |
| SKULDSÄTTNINGSGRAD | Debt-to-equity ratio | Display |
| FÖRESLAGEN UTDELNING | Proposed dividend | Display |
| FRITT EGET KAPITAL | Free equity | Display |
| MATERIELLA ANLÄGGNINGSTILLGÅNGAR | Tangible fixed assets | Display (asset backing) |
| IMMATERIELLA ANLÄGGNINGSTILLGÅNGAR | Intangible fixed assets | Display |
| FINANSIELLA ANLÄGGNINGSTILLGÅNGAR | Financial fixed assets | Display |
