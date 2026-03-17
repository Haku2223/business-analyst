"""Phase 2a — Allabolag /bokslut scraping + historical financial filters.

Scrapes up to 5 fiscal years of financial data from each company's Allabolag
/bokslut page, stores structured JSON in the companies table, and applies
configurable hard + soft trend filters.
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.database import AsyncSessionLocal
from app.models import Company, Phase2Job

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiting constants
# ---------------------------------------------------------------------------
REQUEST_INTERVAL = 0.5   # seconds between requests
JITTER_RANGE = 0.2       # ±0.2s random jitter
MAX_RETRIES = 3           # per-company retry count
RETRY_BACKOFF_BASE = 2.0  # exponential backoff base

# HTTP client defaults
REQUEST_TIMEOUT = 15.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Default Phase 2a filter config
DEFAULT_PHASE2A_CONFIG: dict[str, Any] = {
    # Hard filters
    "p2a_hard_profitability_3of5_enabled": True,
    "p2a_hard_profitability_min_years": 3,

    "p2a_hard_revenue_decline_enabled": True,
    "p2a_hard_revenue_max_decline_pct": 15.0,

    # Soft filters
    "p2a_soft_employee_trend_enabled": True,
    "p2a_soft_employee_shrink_max_pct": 40.0,

    "p2a_soft_revenue_cagr_enabled": True,
    "p2a_soft_revenue_cagr_min_pct": -5.0,

    "p2a_soft_margin_consistency_enabled": True,
    "p2a_soft_margin_min_pct": 5.0,
    "p2a_soft_margin_min_years": 3,
}


# ---------------------------------------------------------------------------
# Global job state (in-process tracking for pause/resume)
# ---------------------------------------------------------------------------
_running_jobs: dict[int, dict] = {}   # job_id → {"task": asyncio.Task, "paused": asyncio.Event}


def get_job_state(job_id: int) -> dict | None:
    """Return in-memory state for a running job, or None."""
    return _running_jobs.get(job_id)


# ---------------------------------------------------------------------------
# HTML/JSON parsing — extract financial data from bokslut page
# ---------------------------------------------------------------------------

def _parse_swedish_number(text: str | None) -> float | None:
    """Parse a Swedish-formatted number string to float.

    Handles formats like '1 234', '1 234,5', '-234', '−234' (unicode minus).
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s or s == "-" or s == "−":
        return None
    # Replace unicode minus
    s = s.replace("−", "-").replace("\u2013", "-")
    # Remove thousands separators (spaces, non-breaking spaces)
    s = s.replace("\xa0", "").replace(" ", "")
    # Swedish decimal comma → dot
    s = s.replace(",", ".")
    # Remove % suffix if present
    s = s.rstrip("%").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_bokslut_nextdata(html: str) -> list[dict[str, Any]]:
    """Extract financial data from the __NEXT_DATA__ JSON embedded in the page.

    Returns a list of dicts, one per fiscal year, newest first.
    """
    soup = BeautifulSoup(html, "lxml")
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if not script_tag or not script_tag.string:
        return []

    try:
        next_data = json.loads(script_tag.string)
    except (json.JSONDecodeError, TypeError):
        return []

    # Navigate the Next.js data structure
    page_props = next_data.get("props", {}).get("pageProps", {})

    # Try various known keys for financial data
    accounts = (
        page_props.get("companyAccounts")
        or page_props.get("accounts")
        or page_props.get("annualAccounts")
        or page_props.get("financialStatements")
    )

    if not accounts:
        # Try deeper nesting
        company = page_props.get("company", {})
        accounts = (
            company.get("companyAccounts")
            or company.get("accounts")
            or company.get("annualAccounts")
        )

    if not accounts or not isinstance(accounts, list):
        return []

    years = []
    for entry in accounts[:5]:  # up to 5 fiscal years
        year_data = _extract_year_from_nextdata(entry)
        if year_data:
            years.append(year_data)

    return years


def _extract_year_from_nextdata(entry: dict) -> dict[str, Any] | None:
    """Extract one fiscal year's data from a __NEXT_DATA__ account entry."""
    if not isinstance(entry, dict):
        return None

    def _get(key: str, *alt_keys: str) -> Any:
        val = entry.get(key)
        if val is not None:
            return val
        for k in alt_keys:
            val = entry.get(k)
            if val is not None:
                return val
        return None

    year = {
        # Section 1: Bokslutsperiod
        "startdatum": _get("periodStart", "startDate", "fiscalYearStart", "fromDate"),
        "slutdatum": _get("periodEnd", "endDate", "fiscalYearEnd", "toDate"),

        # Section 2: Löner & Utdelning
        "valutakod": _get("currency", "currencyCode", "valutakod"),
        "loner_styrelse_vd": _get("boardAndCeoSalaries", "boardCeoSalaries",
                                   "lonerStyrelseVd", "salariesBoardCeo"),
        "loner_ovriga": _get("otherSalaries", "lonerOvriga", "salariesOther"),
        "foreslagen_utdelning": _get("proposedDividend", "dividend",
                                      "foreslagenUtdelning"),

        # Section 3: Resultaträkning
        "nettoomsattning": _get("netRevenue", "netSales", "nettoomsattning"),
        "ovrig_omsattning": _get("otherRevenue", "ovrigOmsattning"),
        "omsattning": _get("revenue", "totalRevenue", "omsattning", "turnover"),
        "lagerforandring": _get("inventoryChange", "lagerforandring"),
        "rorelsekostnader": _get("operatingCosts", "operatingExpenses",
                                  "rorelsekostnader"),
        "rorelseresultat": _get("operatingResult", "operatingProfit",
                                 "rorelseresultat", "ebit"),
        "finansiella_intakter": _get("financialIncome", "finansiellaIntakter"),
        "finansiella_kostnader": _get("financialCosts", "finansiellaKostnader"),
        "resultat_efter_finansnetto": _get("resultAfterFinancialNet",
                                            "resultatEfterFinansnetto",
                                            "profitAfterFinancials"),
        "resultat_fore_skatt": _get("resultBeforeTax", "resultatForeSkatt",
                                     "profitBeforeTax"),
        "skatt": _get("tax", "taxOnYearsResult", "skatt"),
        "arets_resultat": _get("netResult", "netProfit", "netIncome",
                                "aretsResultat", "yearResult"),

        # Section 4: Balansräkning
        "immateriella_anlaggningstillgangar": _get("intangibleAssets",
                                                     "immateriella"),
        "materiella_anlaggningstillgangar": _get("tangibleAssets", "materiella"),
        "finansiella_anlaggningstillgangar": _get("financialAssets",
                                                    "finansiellaAnlaggningstillgangar"),
        "anlaggningstillgangar": _get("totalFixedAssets",
                                       "anlaggningstillgangar"),
        "varulager": _get("inventory", "varulager"),
        "kundfordringar": _get("accountsReceivable", "kundfordringar"),
        "kassa_och_bank": _get("cashAndBank", "kassaOchBank"),
        "omsattningstillgangar": _get("totalCurrentAssets",
                                       "omsattningstillgangar"),
        "summa_tillgangar": _get("totalAssets", "summaTillgangar"),
        "fritt_eget_kapital": _get("freeEquity", "frittEgetKapital",
                                     "unrestrictedEquity"),
        "obeskattade_reserver": _get("untaxedReserves", "obeskattadeReserver"),
        "eget_kapital": _get("equity", "totalEquity", "egetKapital"),
        "avsattningar": _get("provisions", "avsattningar"),
        "langfristiga_skulder": _get("longTermLiabilities",
                                      "langfristigaSkulder"),
        "leverantorsskulder": _get("accountsPayable", "leverantorsskulder"),
        "kortfristiga_skulder": _get("shortTermLiabilities",
                                      "currentLiabilities",
                                      "kortfristigaSkulder"),
        "summa_eget_kapital_och_skulder": _get("totalEquityAndLiabilities",
                                                 "summaEgetKapitalOchSkulder"),

        # Section 5: Nyckeltal
        "vinstmarginal_pct": _get("profitMargin", "vinstmarginal"),
        "kassalikviditet_pct": _get("quickRatio", "kassalikviditet"),
        "soliditet_pct": _get("equityRatio", "soliditet"),
        "skuldsattningsgrad": _get("debtToEquity", "skuldsattningsgrad"),
        "avkastning_eget_kapital_pct": _get("returnOnEquity",
                                              "avkastningEgetKapital"),
        "avkastning_totalt_kapital_pct": _get("returnOnTotalCapital",
                                                "avkastningTotaltKapital"),
        "anstallda": _get("employees", "numberOfEmployees", "anstallda"),
        "personalkostnader_per_anstalld": _get("personnelCostPerEmployee",
                                                 "personalkostnaderPerAnstalld"),
        "ebitda": _get("ebitda", "EBITDA"),
    }

    return year


def parse_bokslut_html_tables(html: str) -> list[dict[str, Any]]:
    """Fallback: parse financial data from HTML tables on the bokslut page.

    This handles the case where __NEXT_DATA__ is not available or doesn't
    contain the financial data.
    """
    soup = BeautifulSoup(html, "lxml")
    years_data: list[dict[str, Any]] = []

    # Find all tables on the page
    tables = soup.find_all("table")
    if not tables:
        return []

    # Section label → field name mapping
    field_map = {
        # Bokslutsperiod
        "startdatum": "startdatum",
        "slutdatum": "slutdatum",
        # Löner & Utdelning
        "valutakod": "valutakod",
        "löner styrelse och vd": "loner_styrelse_vd",
        "löner övriga": "loner_ovriga",
        "föreslagen utdelning": "foreslagen_utdelning",
        # Resultaträkning
        "nettoomsättning": "nettoomsattning",
        "övrig omsättning": "ovrig_omsattning",
        "omsättning": "omsattning",
        "lagerförändring": "lagerforandring",
        "rörelsekostnader": "rorelsekostnader",
        "rörelseresultat efter avskrivningar": "rorelseresultat",
        "finansiella intäkter": "finansiella_intakter",
        "finansiella kostnader": "finansiella_kostnader",
        "resultat efter finansnetto": "resultat_efter_finansnetto",
        "resultat före skatt": "resultat_fore_skatt",
        "skatt på årets resultat": "skatt",
        "årets resultat": "arets_resultat",
        # Balansräkning
        "immateriella anläggningstillgångar": "immateriella_anlaggningstillgangar",
        "materiella anläggningstillgångar": "materiella_anlaggningstillgangar",
        "finansiella anläggningstillgångar": "finansiella_anlaggningstillgangar",
        "anläggningstillgångar": "anlaggningstillgangar",
        "varulager": "varulager",
        "kundfordringar": "kundfordringar",
        "kassa och bank": "kassa_och_bank",
        "omsättningstillgångar": "omsattningstillgangar",
        "summa tillgångar": "summa_tillgangar",
        "fritt eget kapital": "fritt_eget_kapital",
        "obeskattade reserver": "obeskattade_reserver",
        "eget kapital": "eget_kapital",
        "avsättningar": "avsattningar",
        "långfristiga skulder": "langfristiga_skulder",
        "leverantörsskulder": "leverantorsskulder",
        "kortfristiga skulder": "kortfristiga_skulder",
        "summa eget kapital och skulder": "summa_eget_kapital_och_skulder",
        # Nyckeltal
        "vinstmarginal i %": "vinstmarginal_pct",
        "vinstmarginal": "vinstmarginal_pct",
        "kassalikviditet i %": "kassalikviditet_pct",
        "kassalikviditet": "kassalikviditet_pct",
        "soliditet i %": "soliditet_pct",
        "soliditet": "soliditet_pct",
        "skuldsättningsgrad": "skuldsattningsgrad",
        "avkastning eget kapital i %": "avkastning_eget_kapital_pct",
        "avkastning totalt kapital i %": "avkastning_totalt_kapital_pct",
        "anställda": "anstallda",
        "personalkostnader per anställd": "personalkostnader_per_anstalld",
        "ebitda": "ebitda",
    }

    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Try to identify the header row (contains year labels)
        header_row = rows[0]
        header_cells = header_row.find_all(["th", "td"])
        if len(header_cells) < 2:
            continue

        # Extract year labels from header (skip the first cell = label column)
        year_labels = []
        for cell in header_cells[1:]:
            text = cell.get_text(strip=True)
            if text:
                year_labels.append(text)

        if not year_labels:
            continue

        # Ensure we have year_data dicts for each column
        while len(years_data) < len(year_labels):
            years_data.append({})

        # Parse data rows
        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue

            label_text = cells[0].get_text(strip=True).lower().strip()
            # Strip trailing (ksek), (%) etc.
            label_text = re.sub(r"\s*\(.*?\)\s*$", "", label_text).strip()

            field_name = field_map.get(label_text)
            if not field_name:
                # Try partial match
                for key, fname in field_map.items():
                    if key in label_text or label_text in key:
                        field_name = fname
                        break

            if not field_name:
                continue

            # Extract values for each year column
            for i, cell in enumerate(cells[1:]):
                if i >= len(years_data):
                    break
                text = cell.get_text(strip=True)
                if field_name in ("startdatum", "slutdatum", "valutakod"):
                    years_data[i][field_name] = text if text else None
                else:
                    years_data[i][field_name] = _parse_swedish_number(text)

    # Filter out empty dicts
    return [y for y in years_data if y]


def parse_bokslut(html: str) -> list[dict[str, Any]]:
    """Parse financial data from a bokslut page using the best available method.

    Tries __NEXT_DATA__ JSON first, then falls back to HTML table parsing.
    Returns a list of dicts (one per fiscal year, up to 5), newest first.
    """
    # Try JSON extraction first
    years = parse_bokslut_nextdata(html)
    if years:
        return years[:5]

    # Fallback to HTML table parsing
    years = parse_bokslut_html_tables(html)
    return years[:5]


# ---------------------------------------------------------------------------
# Phase 2a filters — applied to historical financial data
# ---------------------------------------------------------------------------

def apply_phase2a_filters(
    historical: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Apply Phase 2a hard and soft filters to historical financial data.

    Returns:
        {
            "passed": bool,
            "hard_failed": list of filter names that hard-failed,
            "soft_failed": list of soft filter names that failed,
            "insufficient_history": bool,
            "details": dict with filter computation details,
        }
    """
    result: dict[str, Any] = {
        "passed": True,
        "hard_failed": [],
        "soft_failed": [],
        "insufficient_history": len(historical) < 3,
        "details": {},
    }

    num_years = len(historical)

    # ----- HARD FILTER 1: Profitability 3 of 5 years -----
    if config.get("p2a_hard_profitability_3of5_enabled", True):
        min_profitable_years = int(config.get("p2a_hard_profitability_min_years", 3))
        net_results = []
        for year in historical:
            ar = year.get("arets_resultat")
            if ar is not None:
                net_results.append(ar)

        profitable_count = sum(1 for r in net_results if r > 0)
        result["details"]["profitable_years"] = profitable_count
        result["details"]["total_years_with_data"] = len(net_results)

        if num_years >= 3:  # Only apply if we have enough data
            if profitable_count < min_profitable_years:
                result["hard_failed"].append("profitability_3of5")
                result["passed"] = False

    # ----- HARD FILTER 2: Revenue decline ≤15% in any 2-year period -----
    if config.get("p2a_hard_revenue_decline_enabled", True):
        max_decline_pct = float(config.get("p2a_hard_revenue_max_decline_pct", 15.0))
        revenues = []
        for year in historical:
            rev = year.get("omsattning") or year.get("nettoomsattning")
            revenues.append(rev)

        max_decline_found = 0.0
        decline_periods: list[dict] = []
        # Revenues are ordered newest first, so index 0 is most recent
        # Check consecutive 2-year periods (older to newer)
        for i in range(len(revenues) - 1, 0, -1):
            older = revenues[i]
            newer = revenues[i - 1]
            if older is not None and newer is not None and older > 0:
                decline = ((older - newer) / abs(older)) * 100
                if decline > 0:  # actual decline
                    decline_periods.append({
                        "from_idx": i,
                        "to_idx": i - 1,
                        "decline_pct": round(decline, 1),
                    })
                    max_decline_found = max(max_decline_found, decline)

        result["details"]["max_revenue_decline_pct"] = round(max_decline_found, 1)
        result["details"]["decline_periods"] = decline_periods

        if max_decline_found > max_decline_pct:
            result["hard_failed"].append("revenue_decline")
            result["passed"] = False

    # ----- SOFT FILTER 1: Employee trend -----
    if config.get("p2a_soft_employee_trend_enabled", True):
        max_shrink_pct = float(config.get("p2a_soft_employee_shrink_max_pct", 40.0))
        employees = [y.get("anstallda") for y in historical]
        valid_employees = [
            (i, e) for i, e in enumerate(employees) if e is not None and e > 0
        ]

        if len(valid_employees) >= 2:
            # oldest is last in list (newest first order)
            oldest_emp = valid_employees[-1][1]
            newest_emp = valid_employees[0][1]
            if oldest_emp > 0:
                shrink_pct = ((oldest_emp - newest_emp) / oldest_emp) * 100
                result["details"]["employee_shrink_pct"] = round(shrink_pct, 1)
                if shrink_pct > max_shrink_pct:
                    result["soft_failed"].append("employee_trend")

    # ----- SOFT FILTER 2: Revenue CAGR -----
    if config.get("p2a_soft_revenue_cagr_enabled", True):
        min_cagr = float(config.get("p2a_soft_revenue_cagr_min_pct", -5.0))
        revenues = []
        for year in historical:
            rev = year.get("omsattning") or year.get("nettoomsattning")
            revenues.append(rev)

        valid_revs = [
            (i, r) for i, r in enumerate(revenues) if r is not None and r > 0
        ]
        if len(valid_revs) >= 2:
            newest_rev = valid_revs[0][1]
            oldest_rev = valid_revs[-1][1]
            years_span = valid_revs[-1][0] - valid_revs[0][0]
            if years_span > 0 and oldest_rev > 0:
                cagr = ((newest_rev / oldest_rev) ** (1.0 / years_span) - 1) * 100
                result["details"]["revenue_cagr_pct"] = round(cagr, 1)
                if cagr < min_cagr:
                    result["soft_failed"].append("revenue_cagr")

    # ----- SOFT FILTER 3: Consistent margin -----
    if config.get("p2a_soft_margin_consistency_enabled", True):
        min_margin = float(config.get("p2a_soft_margin_min_pct", 5.0))
        min_years = int(config.get("p2a_soft_margin_min_years", 3))
        margins = [y.get("vinstmarginal_pct") for y in historical]
        above_threshold = sum(
            1 for m in margins if m is not None and m > min_margin
        )
        result["details"]["margin_above_threshold_years"] = above_threshold
        if above_threshold < min_years:
            result["soft_failed"].append("consistent_margin")

    return result


# ---------------------------------------------------------------------------
# HTTP fetcher — fetch a single bokslut page with retry logic
# ---------------------------------------------------------------------------

async def fetch_bokslut_page(
    client: httpx.AsyncClient,
    url: str,
) -> str:
    """Fetch a bokslut page, retrying up to MAX_RETRIES times on failure.

    Raises an exception if all retries fail.
    """
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7",
                },
                follow_redirects=True,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                logger.warning(
                    "Fetch attempt %d failed for %s: %s — retrying in %.1fs",
                    attempt + 1, url, e, wait,
                )
                await asyncio.sleep(wait)

    raise last_error or RuntimeError(f"All {MAX_RETRIES} retries failed for {url}")


# ---------------------------------------------------------------------------
# Construct bokslut URL
# ---------------------------------------------------------------------------

def bokslut_url(allabolag_url: str | None, orgnr: str) -> str:
    """Build the /bokslut URL for a company.

    Uses the stored allabolag_url if available, otherwise constructs from orgnr.
    """
    if allabolag_url:
        base = allabolag_url.rstrip("/")
        # If it already ends with /bokslut, use as-is
        if base.endswith("/bokslut"):
            return base
        return f"{base}/bokslut"

    # Fallback: construct from org number
    orgnr_clean = orgnr.replace("-", "")
    return f"https://www.allabolag.se/{orgnr_clean}/bokslut"


# ---------------------------------------------------------------------------
# Background job runner
# ---------------------------------------------------------------------------

async def run_phase2a_job(
    job_id: int,
    company_orgnrs: list[str],
    config: dict[str, Any],
) -> None:
    """Run Phase 2a enrichment as a background task.

    Fetches bokslut pages, parses historical financials, applies filters,
    and updates the database. Supports pause/resume via an asyncio.Event.
    """
    pause_event = asyncio.Event()
    pause_event.set()  # not paused initially

    _running_jobs[job_id] = {
        "task": asyncio.current_task(),
        "paused": pause_event,
        "log": [],
    }

    errors: list[dict] = []

    try:
        async with httpx.AsyncClient() as client:
            async with AsyncSessionLocal() as db:
                # Mark job as running
                job = await db.get(Phase2Job, job_id)
                if not job:
                    logger.error("Phase2Job %d not found", job_id)
                    return
                job.status = "running"
                job.started_at = datetime.utcnow()
                job.companies_total = len(company_orgnrs)
                await db.commit()

                companies_done = 0
                # If resuming, skip already-processed companies
                start_idx = 0
                if job.last_completed_orgnr:
                    try:
                        start_idx = company_orgnrs.index(
                            job.last_completed_orgnr
                        ) + 1
                        companies_done = start_idx
                    except ValueError:
                        start_idx = 0

                for i in range(start_idx, len(company_orgnrs)):
                    # Check if paused
                    await pause_event.wait()

                    # Check if job was cancelled (status changed externally)
                    await db.refresh(job)
                    if job.status in ("failed", "completed"):
                        break

                    orgnr = company_orgnrs[i]
                    log_entry = (
                        f"[{i+1}/{len(company_orgnrs)}] Processing {orgnr}"
                    )
                    _log_job(job_id, log_entry)

                    try:
                        company = await db.get(Company, orgnr)
                        if not company:
                            _log_job(
                                job_id,
                                f"  Company {orgnr} not found in DB, skipping",
                            )
                            companies_done += 1
                            continue

                        url = bokslut_url(company.allabolag_url, orgnr)
                        _log_job(job_id, f"  Fetching {url}")

                        html = await fetch_bokslut_page(client, url)
                        historical = parse_bokslut(html)

                        if not historical:
                            _log_job(
                                job_id,
                                f"  No financial data found for {orgnr}",
                            )
                            company.phase2_status = "complete"
                            company.phase2_error = (
                                "No financial data found on page"
                            )
                            company.historical_financials = {
                                "years": [],
                                "phase2a_result": {
                                    "passed": True,
                                    "hard_failed": [],
                                    "soft_failed": [],
                                    "insufficient_history": True,
                                    "details": {},
                                },
                            }
                        else:
                            _log_job(
                                job_id,
                                f"  Found {len(historical)} years of data",
                            )

                            # Apply Phase 2a filters
                            filter_result = apply_phase2a_filters(
                                historical, config
                            )
                            _log_job(
                                job_id,
                                f"  Filters: passed={filter_result['passed']}, "
                                f"hard_failed={filter_result['hard_failed']}, "
                                f"soft_failed={filter_result['soft_failed']}",
                            )

                            company.historical_financials = {
                                "years": historical,
                                "phase2a_result": filter_result,
                            }
                            company.phase2_status = "complete"
                            company.phase2_error = None

                    except Exception as e:
                        error_msg = (
                            f"Error processing {orgnr}: "
                            f"{type(e).__name__}: {e}"
                        )
                        logger.error(error_msg)
                        _log_job(job_id, f"  ERROR: {error_msg}")
                        errors.append({
                            "timestamp": datetime.utcnow().isoformat(),
                            "orgnr": orgnr,
                            "error": str(e),
                        })

                        # Update company with error status
                        company = await db.get(Company, orgnr)
                        if company:
                            company.phase2_status = "failed"
                            company.phase2_error = str(e)

                    # Update job progress
                    companies_done += 1
                    job.companies_done = companies_done
                    job.last_completed_orgnr = orgnr
                    job.errors_json = errors if errors else None
                    await db.commit()

                    # Rate limiting: wait between requests
                    jitter = random.uniform(-JITTER_RANGE, JITTER_RANGE)
                    await asyncio.sleep(max(0.1, REQUEST_INTERVAL + jitter))

                # Mark job complete
                job.status = "completed"
                job.completed_at = datetime.utcnow()
                job.errors_json = errors if errors else None
                await db.commit()

                passed_count = 0
                for orgnr in company_orgnrs:
                    c = await db.get(Company, orgnr)
                    if c and c.historical_financials:
                        p2a = c.historical_financials.get("phase2a_result", {})
                        if p2a.get("passed", False):
                            passed_count += 1

                _log_job(
                    job_id,
                    f"Job complete. {companies_done} processed, "
                    f"{passed_count} passed Phase 2a filters, "
                    f"{len(errors)} errors.",
                )

                # Update batch counts
                if job.batch_id:
                    from app.models import Batch

                    batch = await db.get(Batch, job.batch_id)
                    if batch:
                        batch.row_count_phase2a = passed_count
                        await db.commit()

    except asyncio.CancelledError:
        logger.info("Phase2a job %d was cancelled", job_id)
        async with AsyncSessionLocal() as db:
            job = await db.get(Phase2Job, job_id)
            if job and job.status == "running":
                job.status = "paused"
                job.errors_json = errors if errors else None
                await db.commit()
    except Exception as e:
        logger.error("Phase2a job %d failed: %s", job_id, e, exc_info=True)
        async with AsyncSessionLocal() as db:
            job = await db.get(Phase2Job, job_id)
            if job:
                job.status = "failed"
                job.errors_json = errors if errors else None
                await db.commit()
    finally:
        _running_jobs.pop(job_id, None)


def _log_job(job_id: int, message: str) -> None:
    """Append a log entry to the in-memory job log."""
    state = _running_jobs.get(job_id)
    if state:
        state["log"].append({
            "time": datetime.utcnow().isoformat(),
            "message": message,
        })
        # Keep only the last 500 log entries to prevent memory bloat
        if len(state["log"]) > 500:
            state["log"] = state["log"][-500:]
    logger.info("[Job %d] %s", job_id, message)


# ---------------------------------------------------------------------------
# Job control functions
# ---------------------------------------------------------------------------

async def start_job(
    batch_id: int,
    company_orgnrs: list[str],
    config: dict[str, Any],
) -> int:
    """Create a Phase2Job record and launch the background task.

    Returns the job ID.
    """
    async with AsyncSessionLocal() as db:
        job = Phase2Job(
            batch_id=batch_id,
            status="pending",
            companies_total=len(company_orgnrs),
            companies_done=0,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        job_id = job.id

    # Launch background task
    asyncio.create_task(run_phase2a_job(job_id, company_orgnrs, config))
    return job_id


async def pause_job(job_id: int) -> bool:
    """Pause a running Phase 2a job. Returns True if successful."""
    state = _running_jobs.get(job_id)
    if not state:
        return False
    state["paused"].clear()  # Block the event
    async with AsyncSessionLocal() as db:
        job = await db.get(Phase2Job, job_id)
        if job:
            job.status = "paused"
            await db.commit()
    _log_job(job_id, "Job paused by user")
    return True


async def resume_job(job_id: int) -> bool:
    """Resume a paused Phase 2a job. Returns True if successful."""
    state = _running_jobs.get(job_id)
    if state:
        # Job still in memory — just un-pause it
        state["paused"].set()
        async with AsyncSessionLocal() as db:
            job = await db.get(Phase2Job, job_id)
            if job:
                job.status = "running"
                await db.commit()
        _log_job(job_id, "Job resumed by user")
        return True

    # Job not in memory (e.g. server restarted) — need to re-launch
    async with AsyncSessionLocal() as db:
        job = await db.get(Phase2Job, job_id)
        if not job or job.status not in ("paused", "pending"):
            return False

        # Get the company list for this batch
        from sqlalchemy import select

        from app.models import BatchCompany

        result = await db.execute(
            select(BatchCompany.company_orgnr)
            .where(
                BatchCompany.batch_id == job.batch_id,
                BatchCompany.phase1_passed == True,  # noqa: E712
            )
            .order_by(BatchCompany.id.asc())
        )
        orgnrs = [r[0] for r in result.all()]

        if not orgnrs:
            return False

        job.status = "running"
        await db.commit()

    # Re-launch with the original company list — the runner will skip
    # already-processed companies using last_completed_orgnr
    config = DEFAULT_PHASE2A_CONFIG.copy()
    asyncio.create_task(run_phase2a_job(job_id, orgnrs, config))
    return True


async def get_job_status(job_id: int) -> dict[str, Any] | None:
    """Get the current status of a Phase 2a job."""
    async with AsyncSessionLocal() as db:
        job = await db.get(Phase2Job, job_id)
        if not job:
            return None

        state = _running_jobs.get(job_id)
        log_entries = state["log"][-50:] if state else []

        return {
            "id": job.id,
            "batch_id": job.batch_id,
            "status": job.status,
            "started_at": (
                job.started_at.isoformat() if job.started_at else None
            ),
            "completed_at": (
                job.completed_at.isoformat() if job.completed_at else None
            ),
            "companies_total": job.companies_total,
            "companies_done": job.companies_done,
            "last_completed_orgnr": job.last_completed_orgnr,
            "errors": job.errors_json or [],
            "log": log_entries,
            "progress_pct": (
                round(job.companies_done / job.companies_total * 100, 1)
                if job.companies_total > 0
                else 0
            ),
        }
