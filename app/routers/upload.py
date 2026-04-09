"""GET/POST /upload — file upload, parsing, and Phase 1 trigger."""

import json
import logging

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth import check_auth_redirect, get_display_name
from app.database import get_db
from app.models import Batch, BatchCompany, Company, FilterPreset
from app.services.parser import get_sheet_names, parse_file, df_row_to_company_dict
from app.services.phase1 import DEFAULT_FILTER_CONFIG, run_phase1
from app.routers.filter import get_active_config

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _load_filter_config(config_json: dict | None) -> dict:
    if not config_json:
        return DEFAULT_FILTER_CONFIG.copy()
    cfg = DEFAULT_FILTER_CONFIG.copy()
    cfg.update(config_json)
    return cfg


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    """Render upload page."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)
    filter_config = get_active_config()

    if filter_config == DEFAULT_FILTER_CONFIG:
        from sqlalchemy import select as sa_select
        async for db in get_db():
            result = await db.execute(
                sa_select(FilterPreset).where(FilterPreset.name == "__active__")
            )
            active_row = result.scalars().first()
            if active_row:
                filter_config = DEFAULT_FILTER_CONFIG.copy()
                filter_config.update(active_row.config_json)
            break

    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "display_name": display_name,
            "active_page": "upload",
            "filter_config": filter_config,
            "error": None,
            "success": None,
            "sheets": [],
            "preview_rows": [],
            "preview_cols": [],
        },
    )


@router.post("/upload/sheets", response_class=JSONResponse)
async def get_sheets(request: Request, file: UploadFile = File(...)):
    """Return sheet names for an uploaded Excel file (AJAX)."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    file_bytes = await file.read()
    sheets = get_sheet_names(file_bytes, file.filename or "")
    if not sheets:
        return JSONResponse({"sheets": [], "default_sheet": ""})

    # Determine default sheet
    default = next((s for s in sheets if "allabolag lista" in s.lower()), sheets[0])
    return JSONResponse({"sheets": sheets, "default_sheet": default})


@router.post("/upload", response_class=HTMLResponse)
async def upload_post(
    request: Request,
    file: UploadFile = File(...),
    sheet_name: str = Form(None),
):
    """Handle file upload: parse, run Phase 1, store in DB."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)

    if not file.filename:
        return templates.TemplateResponse(
            "upload.html",
            {
                "request": request,
                "display_name": display_name,
                "active_page": "upload",
                "filter_config": get_active_config(),
                "error": "No file selected.",
                "success": None,
                "sheets": [],
                "preview_rows": [],
                "preview_cols": [],
            },
            status_code=400,
        )

    file_bytes = await file.read()

    # Parse
    try:
        df, warnings = parse_file(file_bytes, file.filename, sheet_name)
    except ValueError as e:
        return templates.TemplateResponse(
            "upload.html",
            {
                "request": request,
                "display_name": display_name,
                "active_page": "upload",
                "filter_config": get_active_config(),
                "error": str(e),
                "success": None,
                "sheets": [],
                "preview_rows": [],
                "preview_cols": [],
            },
            status_code=400,
        )

    row_count_uploaded = len(df)

    # Load filter config (use defaults for now; user can edit on /filter)
    async for db in get_db():
        # Use the active filter config (set via /filter page), falling back to defaults
        filter_config = get_active_config()
        if filter_config == DEFAULT_FILTER_CONFIG:
            result = await db.execute(
                select(FilterPreset).where(FilterPreset.name == "__active__")
            )
            active_row = result.scalars().first()
            if active_row:
                filter_config = DEFAULT_FILTER_CONFIG.copy()
                filter_config.update(active_row.config_json)

        # Run Phase 1 filtering
        phase1_result = run_phase1(df, filter_config)
        passed_df = phase1_result["passed"]
        all_results = phase1_result["all_results"]  # includes failed_filters per row
        row_count_phase1 = len(passed_df)

        # Create batch record
        batch = Batch(
            filename=file.filename,
            filter_config_json=filter_config,
            row_count_uploaded=row_count_uploaded,
            row_count_phase1=row_count_phase1,
        )
        db.add(batch)
        await db.flush()  # get batch.id

        # Upsert companies + create batch_company records
        batch_company_records = []
        for row_dict in all_results:
            orgnr = str(row_dict.get("orgnr", "")).strip()
            if not orgnr or orgnr == "nan":
                continue

            # Check for existing company (cross-batch)
            existing = await db.get(Company, orgnr)
            company_data = df_row_to_company_dict(row_dict)

            if existing:
                # Update financial data but preserve pipeline_status, notes, etc.
                prev_status = existing.pipeline_status
                for k, v in company_data.items():
                    if k not in ("pipeline_status",) and v is not None:
                        setattr(existing, k, v)
                # Preserve pipeline status
                existing.pipeline_status = prev_status
                company = existing
            else:
                company = Company(**company_data)
                db.add(company)

            bc = BatchCompany(
                batch_id=batch.id,
                company_orgnr=orgnr,
                phase1_passed=bool(row_dict.get("_phase1_passed", False)),
                failed_filters=row_dict.get("_failed_filters"),
            )
            batch_company_records.append(bc)

        for bc in batch_company_records:
            db.add(bc)

        await db.commit()

        if warnings:
            logger.warning("Upload warnings for %s: %s", file.filename, warnings)

        return RedirectResponse(
            f"/results?batch_id={batch.id}&uploaded={row_count_uploaded}&passed={row_count_phase1}",
            status_code=302,
        )
