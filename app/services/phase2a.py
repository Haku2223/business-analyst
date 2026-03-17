"""Phase 2a — Allabolag /bokslut scraping + historical filters."""

import asyncio
import json
import logging
import random
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import Company, Phase2Job

logger = logging.getLogger(__name__)

# Rate limiting: 1 request per 0.5s with ±0.2s jitter
BASE_DELAY = 0.5
JITTER = 0.2
MAX_RETRIES = 3

# Default Phase 2a filter config
DEFAULT_PHASE2A_FILTERS = {
    "profitability_3_of_5_enabled": True,
    "profitability_min_years": 3,
    "revenue_decline_enabled": True,
    "revenue_decline_max_pct": 15.0,
    "employee_trend_enabled": True,
    "employee_shrink_max_pct": 40.0,
    "revenue_cagr_enabled": True,
    "revenue_cagr_min_pct": -5.0,
    "consistent_margin_enabled": True,
    "consistent_margin_min_pct": 5.0,
    "consistent_margin_min_years": 3,
}

# In-memory job control: job_id → {"paused": bool, "cancel": bool}
_job_control: dict[int, dict[str, bool]] = {}


async def resolve_bokslut_url(client: httpx.AsyncClient, allabolag_url: str) -> str:
    """
    Resolve the correct /bokslut URL for a company.

    The stored allabolag_url (from Excel hyperlinks) uses the org number as the
    last path segment, but the Allabolag /bokslut page uses an internal ID.
    We send a HEAD request (with redirect following) to the /foretag/ URL to
    discover the canonical URL with the correct internal ID, then derive the
    /bokslut/ URL by path replacement.

    Falls back to naive replacement if the HEAD request fails.
    """
    if not allabolag_url:
        return ""

    url = allabolag_url.rstrip("/")

    # If URL already points to /bokslut/, assume it's correct
    parsed = urlparse(url)
    if parsed.path.startswith("/bokslut/"):
        return url

    # HEAD request to resolve redirects and get canonical URL with internal ID
    try:
        resp = await client.head(url, follow_redirects=True, timeout=15.0)
        resp.raise_for_status()

        # The final URL after redirects contains the correct internal ID
        # e.g., /foretag/slug/city/category/INTERNAL_ID
        final_url = str(resp.url).rstrip("/")

        if "/foretag/" in final_url:
            return final_url.replace("/foretag/", "/bokslut/", 1)

    except Exception as e:
        logger.warning("Could not resolve bokslut URL from company page %s: %s", url, e)

    # Fallback: naive replacement on the original URL
    if "/foretag/" in url:
        return url.replace("/foretag/", "/bokslut/", 1)

    path = parsed.path.lstrip("/")
    return f"https://www.allabolag.se/bokslut/{path}"


def _parse_ksek_value(text: str | None) -> int | None:
    """Parse a KSEK text value to integer (öre). Returns None if unparseable."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d\-\.,]", "", text.strip())
    cleaned = cleaned.replace(",", ".")
    if not cleaned or cleaned == "-":
        return None
    try:
        val = float(cleaned)
        # Value is in KSEK, convert to öre: KSEK * 1000 * 100
        return int(val * 100_000)
    except ValueError:
        return None


def _parse_pct_value(text: str | None) -> float | None:
    """Parse a percentage text value to float."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d\-\.,]", "", text.strip())
    cleaned = cleaned.replace(",", ".")
    if not cleaned or cleaned == "-":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int_value(text: str | None) -> int | None:
    """Parse an integer text value."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d\-]", "", text.strip())
    if not cleaned or cleaned == "-":
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_bokslut_html(html: str) -> list[dict[str, Any]]:
    """
    Parse the /bokslut page HTML and extract financial data for up to 5 fiscal years.

    Returns a list of dicts, one per fiscal year, with all fields from the 5 sections.
    """
    soup = BeautifulSoup(html, "lxml")
    years_data: list[dict[str, Any]] = []

    # Find all financial data tables on the page
    tables = soup.find_all("table")

    if not tables:
        return years_data

    # The bokslut page typically has tables with year columns
    # We'll look for the main data sections and parse them

    # Strategy: find all rows with financial data, organized by section headers
    # The page structure has sections with headers and data rows

    # Find all section containers
    sections = soup.find_all(["section", "div"], class_=re.compile(r"bokslut|financial|annual", re.I))
    if not sections:
        # Fallback: parse the entire page
        sections = [soup]

    # Try to find year headers from the table columns
    year_headers: list[str] = []

    for table in tables:
        header_row = table.find("tr")
        if header_row:
            ths = header_row.find_all(["th", "td"])
            potential_years = []
            for th in ths:
                txt = th.get_text(strip=True)
                # Look for date patterns like "2023-12" or "2022/2023" or just years
                if re.match(r"\d{4}", txt):
                    potential_years.append(txt)
            if len(potential_years) >= 2:
                year_headers = potential_years
                break

    if not year_headers:
        # Try looking for year-like text in any header-style elements
        for el in soup.find_all(["th", "h3", "h4", "span"]):
            txt = el.get_text(strip=True)
            match = re.match(r"(\d{4}[-/]\d{2,4})", txt)
            if match:
                year_headers.append(match.group(1))

    # Initialize year dicts
    num_years = max(len(year_headers), 1)
    num_years = min(num_years, 5)  # Cap at 5 years
    for i in range(num_years):
        year_dict: dict[str, Any] = {}
        if i < len(year_headers):
            year_dict["period"] = year_headers[i]
        years_data.append(year_dict)

    # Field mapping: Swedish label → internal key + parser
    field_map: dict[str, tuple[str, str]] = {
        # Section 1 — Bokslutsperiod
        "startdatum": ("startdatum", "text"),
        "slutdatum": ("slutdatum", "text"),
        # Section 2 — Löner & Utdelning
        "valutakod": ("valutakod", "text"),
        "löner styrelse och vd": ("loner_styrelse_vd", "ksek"),
        "löner övriga": ("loner_ovriga", "ksek"),
        "föreslagen utdelning": ("foreslagen_utdelning", "ksek"),
        # Section 3 — Resultaträkning
        "nettoomsättning": ("nettoomsattning", "ksek"),
        "övrig omsättning": ("ovrig_omsattning", "ksek"),
        "omsättning": ("omsattning", "ksek"),
        "lagerförändring": ("lagerforandring", "ksek"),
        "rörelsekostnader": ("rorelsekostnader", "ksek"),
        "rörelseresultat efter avskrivningar": ("rorelseresultat_efter_avskrivningar", "ksek"),
        "finansiella intäkter": ("finansiella_intakter", "ksek"),
        "finansiella kostnader": ("finansiella_kostnader", "ksek"),
        "resultat efter finansnetto": ("resultat_efter_finansnetto", "ksek"),
        "resultat före skatt": ("resultat_fore_skatt", "ksek"),
        "skatt på årets resultat": ("skatt_arets_resultat", "ksek"),
        "årets resultat": ("arets_resultat", "ksek"),
        # Section 4 — Balansräkning
        "immateriella anläggningstillgångar": ("immateriella_anlaggningstillgangar", "ksek"),
        "materiella anläggningstillgångar": ("materiella_anlaggningstillgangar", "ksek"),
        "finansiella anläggningstillgångar": ("finansiella_anlaggningstillgangar", "ksek"),
        "anläggningstillgångar": ("anlaggningstillgangar", "ksek"),
        "varulager": ("varulager", "ksek"),
        "kundfordringar": ("kundfordringar", "ksek"),
        "kassa och bank": ("kassa_och_bank", "ksek"),
        "omsättningstillgångar": ("omsattningstillgangar", "ksek"),
        "summa tillgångar": ("summa_tillgangar", "ksek"),
        "fritt eget kapital": ("fritt_eget_kapital", "ksek"),
        "obeskattade reserver": ("obeskattade_reserver", "ksek"),
        "eget kapital": ("eget_kapital", "ksek"),
        "avsättningar": ("avsattningar", "ksek"),
        "långfristiga skulder": ("langfristiga_skulder", "ksek"),
        "leverantörsskulder": ("leverantorsskulder", "ksek"),
        "kortfristiga skulder": ("kortfristiga_skulder", "ksek"),
        "summa eget kapital och skulder": ("summa_eget_kapital_och_skulder", "ksek"),
        # Section 5 — Nyckeltal
        "vinstmarginal i %": ("vinstmarginal", "pct"),
        "vinstmarginal": ("vinstmarginal", "pct"),
        "kassalikviditet i %": ("kassalikviditet", "pct"),
        "kassalikviditet": ("kassalikviditet", "pct"),
        "soliditet i %": ("soliditet", "pct"),
        "soliditet": ("soliditet", "pct"),
        "skuldsättningsgrad": ("skuldsattningsgrad", "pct"),
        "avkastning eget kapital i %": ("avkastning_eget_kapital", "pct"),
        "avkastning totalt kapital i %": ("avkastning_totalt_kapital", "pct"),
        "anställda": ("anstallda", "int"),
        "personalkostnader per anställd (ksek)": ("personalkostnader_per_anstalld", "ksek"),
        "personalkostnader per anställd": ("personalkostnader_per_anstalld", "ksek"),
        "ebitda (ksek)": ("ebitda", "ksek"),
        "ebitda": ("ebitda", "ksek"),
    }

    # Parse each table row
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            # First cell is usually the label
            label_text = cells[0].get_text(strip=True).lower()

            # Look up the label in our field map
            matched_key = None
            matched_type = None
            for pattern, (key, vtype) in field_map.items():
                if pattern in label_text or label_text in pattern:
                    matched_key = key
                    matched_type = vtype
                    break

            if not matched_key:
                continue

            # Remaining cells are values for each year
            value_cells = cells[1:]
            for i, cell in enumerate(value_cells):
                if i >= len(years_data):
                    break
                raw_val = cell.get_text(strip=True)
                if matched_type == "ksek":
                    years_data[i][matched_key] = _parse_ksek_value(raw_val)
                elif matched_type == "pct":
                    years_data[i][matched_key] = _parse_pct_value(raw_val)
                elif matched_type == "int":
                    years_data[i][matched_key] = _parse_int_value(raw_val)
                else:
                    years_data[i][matched_key] = raw_val if raw_val and raw_val != "-" else None

    # Remove empty year entries
    years_data = [y for y in years_data if len(y) > 1 or (len(y) == 1 and "period" not in y)]

    return years_data


def apply_phase2a_filters(
    historical: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> tuple[bool, list[str], list[str]]:
    """
    Apply Phase 2a hard and soft filters on historical financial data.

    Returns:
        (passed, hard_fails, soft_fails) where passed is True if all hard filters pass.
    """
    cfg = DEFAULT_PHASE2A_FILTERS.copy()
    if config:
        cfg.update(config)

    hard_fails: list[str] = []
    soft_fails: list[str] = []

    num_years = len(historical)

    if num_years < 3:
        # Insufficient history — skip profitability filter, don't hard-fail
        soft_fails.append("insufficient_history")
    else:
        # Hard: Profitability 3/5 years
        if cfg.get("profitability_3_of_5_enabled", True):
            min_years = cfg.get("profitability_min_years", 3)
            profitable_years = sum(
                1 for y in historical
                if y.get("arets_resultat") is not None and y["arets_resultat"] > 0
            )
            if profitable_years < min_years:
                hard_fails.append(f"profitability_{min_years}_of_{num_years}")

        # Hard: Revenue decline max 15% over any 2-year consecutive period
        if cfg.get("revenue_decline_enabled", True):
            max_decline = cfg.get("revenue_decline_max_pct", 15.0)
            revenues = [y.get("omsattning") or y.get("nettoomsattning") for y in historical]
            for i in range(len(revenues) - 1):
                if revenues[i] is not None and revenues[i + 1] is not None and revenues[i] > 0:
                    decline_pct = (revenues[i] - revenues[i + 1]) / revenues[i] * 100
                    if decline_pct > max_decline:
                        hard_fails.append("revenue_decline_exceeded")
                        break

    # Soft filters
    if num_years >= 2:
        # Soft: Employee trend
        if cfg.get("employee_trend_enabled", True):
            max_shrink = cfg.get("employee_shrink_max_pct", 40.0)
            employees = [y.get("anstallda") for y in historical]
            first_emp = next((e for e in employees if e is not None and e > 0), None)
            last_emp = next((e for e in reversed(employees) if e is not None), None)
            if first_emp and last_emp and first_emp > 0:
                shrink_pct = (first_emp - last_emp) / first_emp * 100
                if shrink_pct > max_shrink:
                    soft_fails.append("employee_shrink")

        # Soft: Revenue CAGR
        if cfg.get("revenue_cagr_enabled", True) and num_years >= 2:
            min_cagr = cfg.get("revenue_cagr_min_pct", -5.0)
            revenues = [y.get("omsattning") or y.get("nettoomsattning") for y in historical]
            first_rev = next((r for r in revenues if r is not None and r > 0), None)
            last_rev = next((r for r in reversed(revenues) if r is not None and r > 0), None)
            if first_rev and last_rev and first_rev > 0:
                n = num_years - 1
                if n > 0:
                    cagr = ((last_rev / first_rev) ** (1 / n) - 1) * 100
                    if cagr < min_cagr:
                        soft_fails.append("revenue_cagr_low")

        # Soft: Consistent margin
        if cfg.get("consistent_margin_enabled", True):
            min_margin = cfg.get("consistent_margin_min_pct", 5.0)
            min_margin_years = cfg.get("consistent_margin_min_years", 3)
            margins = [y.get("vinstmarginal") for y in historical]
            good_margin_years = sum(
                1 for m in margins if m is not None and m > min_margin
            )
            if good_margin_years < min_margin_years:
                soft_fails.append("inconsistent_margin")

    passed = len(hard_fails) == 0
    return passed, hard_fails, soft_fails


async def scrape_company_bokslut(
    client: httpx.AsyncClient,
    company: Company,
) -> dict[str, Any]:
    """
    Scrape the /bokslut page for a single company.

    First resolves the correct bokslut URL (which uses an internal Allabolag ID,
    different from the org number in the stored URL), then fetches and parses it.

    Returns a dict with keys: "historical", "error", "url_used".
    """
    if not company.allabolag_url:
        return {"historical": None, "error": "No Allabolag URL", "url_used": ""}

    # Step 1: Resolve the correct bokslut URL (needs HTTP request to company page)
    try:
        bokslut_url = await resolve_bokslut_url(client, company.allabolag_url)
    except Exception as e:
        return {"historical": None, "error": f"URL resolution failed: {e}", "url_used": company.allabolag_url}

    if not bokslut_url:
        return {"historical": None, "error": "Could not resolve bokslut URL", "url_used": company.allabolag_url}

    logger.info("Resolved bokslut URL for %s: %s", company.orgnr, bokslut_url)

    # Step 2: Fetch the bokslut page
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(
                bokslut_url,
                follow_redirects=True,
                timeout=15.0,
            )
            resp.raise_for_status()
            historical = parse_bokslut_html(resp.text)
            return {"historical": historical, "error": None, "url_used": bokslut_url}
        except Exception as e:
            last_error = str(e)
            logger.warning(
                "Scrape attempt %d/%d failed for %s: %s",
                attempt + 1, MAX_RETRIES, company.orgnr, last_error,
            )
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff

    return {"historical": None, "error": f"Fetch failed after {MAX_RETRIES} attempts: {last_error}", "url_used": bokslut_url}


async def run_phase2a_job(
    job_id: int,
    company_orgnrs: list[str],
    filter_config: dict[str, Any] | None = None,
) -> None:
    """
    Run Phase 2a enrichment for the given companies as a background task.

    Updates the Phase2Job record with progress. Supports pause/resume via _job_control.
    """
    _job_control[job_id] = {"paused": False, "cancel": False}

    async with AsyncSessionLocal() as db:
        job = await db.get(Phase2Job, job_id)
        if not job:
            logger.error("Phase2Job %d not found", job_id)
            return

        job.status = "running"
        job.started_at = datetime.utcnow()
        job.companies_total = len(company_orgnrs)
        await db.commit()

        # Determine resume point
        start_index = 0
        if job.last_completed_orgnr:
            try:
                idx = company_orgnrs.index(job.last_completed_orgnr)
                start_index = idx + 1
            except ValueError:
                start_index = 0

        errors: list[dict[str, str]] = list(job.errors_json or [])
        companies_done = job.companies_done or 0

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
        }

        async with httpx.AsyncClient(headers=headers) as client:
            for i in range(start_index, len(company_orgnrs)):
                # Check for pause/cancel
                ctrl = _job_control.get(job_id, {})
                if ctrl.get("cancel"):
                    job.status = "paused"
                    await db.commit()
                    return

                while ctrl.get("paused"):
                    await asyncio.sleep(1)
                    ctrl = _job_control.get(job_id, {})
                    if ctrl.get("cancel"):
                        job.status = "paused"
                        await db.commit()
                        return

                orgnr = company_orgnrs[i]
                company = await db.get(Company, orgnr)
                if not company:
                    continue

                # Rate limiting with jitter
                delay = BASE_DELAY + random.uniform(-JITTER, JITTER)
                await asyncio.sleep(delay)

                # Scrape
                result = await scrape_company_bokslut(client, company)

                if result["error"]:
                    company.phase2_status = "failed"
                    company.phase2_error = result["error"]
                    errors.append({
                        "timestamp": datetime.utcnow().isoformat(),
                        "orgnr": orgnr,
                        "error": result["error"],
                        "url": result["url_used"],
                    })
                elif result["historical"] is not None:
                    # Apply Phase 2a filters
                    passed, hard_fails, soft_fails = apply_phase2a_filters(
                        result["historical"], filter_config
                    )
                    company.historical_financials = result["historical"]
                    company.phase2_status = "complete"
                    company.phase2_error = None

                    # Store filter results in extra_data
                    extra = company.extra_data or {}
                    extra["phase2a_passed"] = passed
                    extra["phase2a_hard_fails"] = hard_fails
                    extra["phase2a_soft_fails"] = soft_fails
                    company.extra_data = extra

                companies_done += 1
                job.companies_done = companies_done
                job.last_completed_orgnr = orgnr
                job.errors_json = errors
                await db.commit()

                logger.info(
                    "Phase2a progress: %d/%d (orgnr=%s)",
                    companies_done, len(company_orgnrs), orgnr,
                )

        # Mark job complete
        job.status = "completed"
        job.completed_at = datetime.utcnow()
        job.errors_json = errors
        await db.commit()

    # Clean up control
    _job_control.pop(job_id, None)
    logger.info("Phase2a job %d completed: %d/%d companies", job_id, companies_done, len(company_orgnrs))


def pause_job(job_id: int) -> None:
    """Pause a running Phase 2a job."""
    if job_id in _job_control:
        _job_control[job_id]["paused"] = True


def resume_job(job_id: int) -> None:
    """Resume a paused Phase 2a job."""
    if job_id in _job_control:
        _job_control[job_id]["paused"] = False


def cancel_job(job_id: int) -> None:
    """Cancel/stop a running Phase 2a job."""
    if job_id in _job_control:
        _job_control[job_id]["cancel"] = True
