"""GET /results — Phase 1 results table."""

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth import check_auth_redirect, get_display_name
from app.database import get_db
from app.models import Batch, BatchCompany, Company

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

PAGE_SIZE = 50


def _öre_to_sek(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v) / 100
    except (TypeError, ValueError):
        return None


def _öre_to_ksek(v) -> float | None:
    s = _öre_to_sek(v)
    if s is None:
        return None
    return s / 1000


def _calc_age(reg_date_str: str | None) -> int | None:
    if not reg_date_str:
        return None
    try:
        d = date.fromisoformat(str(reg_date_str))
        return (date.today() - d).days // 365
    except (ValueError, TypeError):
        return None


def _soft_flag_count(failed_filters: list | None) -> int:
    if not failed_filters:
        return 0
    return sum(1 for f in failed_filters if str(f).startswith("soft:"))


def _sni_display(sni_codes: str | None, sni_names: str | None) -> str:
    """Return the first SNI code + name pair for display."""
    if not sni_codes:
        return ""
    codes = [c.strip() for c in str(sni_codes).split(",") if c.strip()]
    names = [n.strip() for n in str(sni_names or "").split(",") if n.strip()]
    if not codes:
        return ""
    first_code = codes[0]
    first_name = names[0] if names else ""
    if first_name:
        return f"{first_code} – {first_name[:30]}"
    return first_code


@router.get("/results", response_class=HTMLResponse)
async def results_page(
    request: Request,
    batch_id: int = None,
    page: int = 1,
    uploaded: int = None,
    passed: int = None,
):
    """Render Phase 1 results table."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)

    async for db in get_db():
        # Load all batches for the filter dropdown
        batches_result = await db.execute(
            select(Batch).order_by(Batch.upload_timestamp.desc())
        )
        all_batches = batches_result.scalars().all()

        # If no batch_id specified, use the most recent
        if batch_id is None and all_batches:
            batch_id = all_batches[0].id

        # Build query: join batch_companies with companies
        query = (
            select(BatchCompany, Company)
            .join(Company, BatchCompany.company_orgnr == Company.orgnr)
            .order_by(BatchCompany.id.asc())
        )
        if batch_id:
            query = query.where(BatchCompany.batch_id == batch_id)

        result = await db.execute(query)
        pairs = result.all()

        # Get previously-passed orgnrs (pipeline_status == 'pass' in earlier batches)
        # We check the Company table directly
        all_orgnrs = {c.orgnr for _, c in pairs}
        pass_result = await db.execute(
            select(Company.orgnr).where(
                Company.orgnr.in_(all_orgnrs),
                Company.pipeline_status == "pass",
            )
        )
        previously_passed_orgnrs = {r[0] for r in pass_result.all()}

        # Build row dicts for template
        rows = []
        for bc, company in pairs:
            row = {
                "orgnr": company.orgnr,
                "bolagsnamn": company.bolagsnamn,
                "allabolag_url": company.allabolag_url,
                "ort": company.ort,
                "lan": company.lan,
                "sni_codes": company.sni_codes,
                "sni_names": company.sni_names,
                "sni_display": _sni_display(company.sni_codes, company.sni_names),
                "age_years": _calc_age(company.registreringsdatum),
                "omsattning_sek": _öre_to_sek(company.omsattning),
                "antal_anstallda": company.antal_anstallda,
                "arets_resultat_ksek": _öre_to_ksek(company.arets_resultat),
                "vinstmarginal": company.vinstmarginal,
                "soliditet": company.soliditet,
                "loner_ksek": _öre_to_ksek(company.loner_styrelse_vd),
                "kassa_ksek": _öre_to_ksek(company.kassa_och_bank),
                "ordforande": company.ordforande,
                "vd": company.vd,
                "hemsida": company.hemsida,
                "pipeline_status": company.pipeline_status or "unreviewed",
                "phase2_status": company.phase2_status or "not_started",
                "phase1_passed": bc.phase1_passed,
                "failed_filters": bc.failed_filters or [],
                "soft_flags": _soft_flag_count(bc.failed_filters),
                "batch_id": bc.batch_id,
                "previously_passed": (
                    company.orgnr in previously_passed_orgnrs
                    and batch_id is not None
                    and bc.batch_id == batch_id
                ),
            }
            rows.append(row)

        # Pagination
        total_count = uploaded or len(rows)
        passed_count = passed or sum(1 for r in rows if r["phase1_passed"])
        total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * PAGE_SIZE
        page_rows = rows[offset: offset + PAGE_SIZE]

        message = None
        if uploaded is not None and passed is not None:
            message = (
                f"Upload complete: {uploaded} rows processed, "
                f"{passed} companies passed Phase 1 filters."
            )

        return templates.TemplateResponse(
            "results.html",
            {
                "request": request,
                "display_name": display_name,
                "active_page": "results",
                "rows": page_rows,
                "total_count": total_count,
                "passed_count": passed_count,
                "page": page,
                "total_pages": total_pages,
                "batch_id": batch_id,
                "all_batches": all_batches,
                "message": message,
            },
        )
