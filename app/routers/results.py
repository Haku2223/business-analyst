"""GET /results — Phase 1 results table + refilter API."""

import json
import logging
from datetime import date, timedelta

import pandas as pd
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth import check_auth_redirect, get_display_name
from app.database import get_db
from app.models import Batch, BatchCompany, Company, FilterPreset
from app.services.phase1 import DEFAULT_FILTER_CONFIG, run_phase1

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
                "bolagstyp": company.bolagstyp,
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

        # Sort rows: passed companies first, then failed — so pagination
        # groups all passed companies on the earliest pages.
        rows.sort(key=lambda r: (not r["phase1_passed"], r.get("orgnr", "")))

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

        # Load the batch's filter config for the inline filter panel
        batch_filter_cfg = DEFAULT_FILTER_CONFIG.copy()
        batch_filter_cfg["filter_types"] = DEFAULT_FILTER_CONFIG["filter_types"].copy()
        if batch_id:
            current_batch = await db.get(Batch, batch_id)
            if current_batch and current_batch.filter_config_json:
                stored = current_batch.filter_config_json
                stored_types = stored.get("filter_types", {})
                batch_filter_cfg.update(stored)
                batch_filter_cfg["filter_types"] = {
                    **DEFAULT_FILTER_CONFIG["filter_types"],
                    **stored_types,
                }

        # Load presets for the filter panel dropdown
        presets_result = await db.execute(
            select(FilterPreset).order_by(FilterPreset.created_at.desc())
        )
        presets = presets_result.scalars().all()

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
                "cfg": batch_filter_cfg,
                "presets": presets,
            },
        )


def _company_to_df_row(company: Company) -> dict:
    """Convert a Company ORM object to a dict matching the DataFrame schema used by phase1."""
    return {
        "orgnr": company.orgnr,
        "bolagsnamn": company.bolagsnamn,
        "bolagstyp": company.bolagstyp,
        "registreringsdatum": company.registreringsdatum,
        "antal_anstallda": company.antal_anstallda,
        "omsattning": company.omsattning,
        "arets_resultat": company.arets_resultat,
        "vinstmarginal": company.vinstmarginal,
        "soliditet": company.soliditet,
        "hemsida": company.hemsida,
        "ordforande": company.ordforande,
        "vd": company.vd,
        "ort": company.ort,
        "lan": company.lan,
        "bokslutsperiod_slut": company.bokslutsperiod_slut,
        "sni_codes": company.sni_codes,
        "sni_names": company.sni_names,
        "allabolag_url": company.allabolag_url,
        "aktiekapital": company.aktiekapital,
        "eget_kapital": company.eget_kapital,
        "summa_tillgangar": company.summa_tillgangar,
        "kassa_och_bank": company.kassa_och_bank,
        "loner_styrelse_vd": company.loner_styrelse_vd,
        "kassalikviditet": company.kassalikviditet,
        "skuldsattningsgrad": company.skuldsattningsgrad,
    }


@router.post("/api/batch/{batch_id}/refilter", response_class=JSONResponse)
async def refilter_batch(request: Request, batch_id: int):
    """Re-run Phase 1 filters on an existing batch with a new filter config."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    new_config = body.get("config", {})

    # Merge with defaults so all keys are present; deep-merge filter_types
    filter_config = DEFAULT_FILTER_CONFIG.copy()
    filter_config["filter_types"] = DEFAULT_FILTER_CONFIG["filter_types"].copy()
    new_types = new_config.get("filter_types", {})
    filter_config.update({k: v for k, v in new_config.items() if k != "filter_types"})
    filter_config["filter_types"].update(new_types)

    async for db in get_db():
        batch = await db.get(Batch, batch_id)
        if not batch:
            return JSONResponse({"error": "Batch not found"}, status_code=404)

        # Load all companies for this batch
        query = (
            select(BatchCompany, Company)
            .join(Company, BatchCompany.company_orgnr == Company.orgnr)
            .where(BatchCompany.batch_id == batch_id)
            .order_by(BatchCompany.id.asc())
        )
        result = await db.execute(query)
        pairs = result.all()

        if not pairs:
            return JSONResponse({"error": "No companies in this batch"}, status_code=404)

        # Build DataFrame from company DB records
        rows_data = [_company_to_df_row(company) for _, company in pairs]
        df = pd.DataFrame(rows_data)

        # Run Phase 1 with the new config
        phase1_result = run_phase1(df, filter_config)
        all_results = phase1_result["all_results"]
        passed_count = len(phase1_result["passed"])

        # Build a lookup from orgnr → new filter results
        results_by_orgnr = {}
        for row_dict in all_results:
            orgnr = str(row_dict.get("orgnr", "")).strip()
            if orgnr and orgnr != "nan":
                results_by_orgnr[orgnr] = row_dict

        # Update batch_companies records
        for bc, company in pairs:
            row_dict = results_by_orgnr.get(company.orgnr)
            if row_dict:
                bc.phase1_passed = bool(row_dict.get("_phase1_passed", False))
                bc.failed_filters = row_dict.get("_failed_filters", [])

        # Update batch metadata
        batch.filter_config_json = filter_config
        batch.row_count_phase1 = passed_count

        await db.commit()

        return JSONResponse({
            "ok": True,
            "batch_id": batch_id,
            "total": len(pairs),
            "passed": passed_count,
        })


@router.get("/api/preset/{preset_id}", response_class=JSONResponse)
async def get_preset(request: Request, preset_id: int):
    """Return a filter preset's config as JSON."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    async for db in get_db():
        preset = await db.get(FilterPreset, preset_id)
        if not preset:
            return JSONResponse({"error": "Preset not found"}, status_code=404)
        return JSONResponse(preset.config_json)
