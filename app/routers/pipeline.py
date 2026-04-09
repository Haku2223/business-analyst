"""GET /pipeline — Kanban board with batch/list toggle."""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth import check_auth_redirect, get_display_name
from app.database import get_db
from app.models import Batch, BatchCompany, Company, PipelineEvent

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

STATUSES = ["unreviewed", "watch", "deep_dive", "pass"]


def _öre_to_msek(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(float(v) / 100 / 1_000_000, 1)
    except (TypeError, ValueError):
        return None


def _company_to_card(company: Company, batch_names: list[str]) -> dict:
    """Convert a Company ORM object to a Kanban card dict."""
    desc = company.ai_description or ""
    if len(desc) > 130:
        desc = desc[:127] + "…"

    return {
        "orgnr": company.orgnr,
        "bolagsnamn": company.bolagsnamn or company.orgnr,
        "ort": company.ort or "",
        "lan": company.lan or "",
        "omsattning_msek": _öre_to_msek(company.omsattning),
        "antal_anstallda": company.antal_anstallda,
        "vinstmarginal": company.vinstmarginal,
        "soliditet": company.soliditet,
        "ai_description": desc,
        "pipeline_status": company.pipeline_status or "unreviewed",
        "phase2_status": company.phase2_status or "not_started",
        "allabolag_url": company.allabolag_url or "",
        "batch_names": batch_names,
    }


@router.get("/pipeline", response_class=HTMLResponse)
async def pipeline_page(
    request: Request,
    batch_id: int = None,
    show_all: bool = False,
    show_phase1: bool = False,
):
    """Kanban board: list toggle, per-column cards, drag-and-drop.

    Single-list view filter modes (mutually exclusive):
    - default (neither flag):  phase1_passed=True AND phase2_status='complete'
    - show_phase1=True:        phase1_passed=True (regardless of Phase 2)
    - show_all=True:           all companies in the batch
    """
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)

    async for db in get_db():
        # ---- Load all batches for the list toggle tabs ----
        batches_result = await db.execute(
            select(Batch).order_by(Batch.upload_timestamp.desc())
        )
        all_batches = batches_result.scalars().all()

        # ---- Load batch→name lookup (used for tooltip) ----
        batch_name_map: dict[int, str] = {
            b.id: (b.list_name or b.filename) for b in all_batches
        }

        if batch_id is not None:
            # Single list view: companies from the chosen batch
            query = (
                select(BatchCompany, Company)
                .join(Company, BatchCompany.company_orgnr == Company.orgnr)
                .where(BatchCompany.batch_id == batch_id)
                .order_by(Company.bolagsnamn.asc())
            )
            if show_all:
                pass  # no phase filter — show everything in the batch
            elif show_phase1:
                # Phase 1 survivors, regardless of Phase 2 status
                query = query.where(BatchCompany.phase1_passed == True)  # noqa: E712
            else:
                # Default: passed both Phase 1 and Phase 2
                query = query.where(BatchCompany.phase1_passed == True)  # noqa: E712
                query = query.where(Company.phase2_status == "complete")

            result = await db.execute(query)
            pairs = result.all()
            company_orgnrs = [c.orgnr for _, c in pairs]

            # For tooltip: find all batches each company belongs to
            if company_orgnrs:
                bc_all = await db.execute(
                    select(BatchCompany).where(
                        BatchCompany.company_orgnr.in_(company_orgnrs)
                    )
                )
                orgnr_batch_ids: dict[str, list[int]] = {}
                for bc in bc_all.scalars().all():
                    orgnr_batch_ids.setdefault(bc.company_orgnr, []).append(bc.batch_id)
            else:
                orgnr_batch_ids = {}

            cards = [
                _company_to_card(
                    c,
                    [batch_name_map.get(bid, f"List {bid}") for bid in orgnr_batch_ids.get(c.orgnr, [batch_id])],
                )
                for _, c in pairs
            ]

            current_batch = await db.get(Batch, batch_id)

        else:
            # All lists view: every company in the DB, deduped by orgnr
            companies_result = await db.execute(
                select(Company).order_by(Company.bolagsnamn.asc())
            )
            companies = companies_result.scalars().all()

            # Build orgnr → list of batch names for tooltip
            bc_all = await db.execute(select(BatchCompany))
            orgnr_batch_ids: dict[str, list[int]] = {}
            for bc in bc_all.scalars().all():
                orgnr_batch_ids.setdefault(bc.company_orgnr, []).append(bc.batch_id)

            cards = [
                _company_to_card(
                    c,
                    list(dict.fromkeys(  # dedupe while preserving order
                        batch_name_map.get(bid, f"List {bid}")
                        for bid in orgnr_batch_ids.get(c.orgnr, [])
                    )),
                )
                for c in companies
            ]

            current_batch = None

        # Group cards by pipeline status
        columns: dict[str, list[dict]] = {s: [] for s in STATUSES}
        for card in cards:
            status = card["pipeline_status"]
            if status not in columns:
                status = "unreviewed"
            columns[status].append(card)

        total_cards = sum(len(v) for v in columns.values())

        return templates.TemplateResponse(
            "pipeline.html",
            {
                "request": request,
                "display_name": display_name,
                "active_page": "pipeline",
                "all_batches": all_batches,
                "current_batch": current_batch,
                "batch_id": batch_id,
                "show_all": show_all,
                "show_phase1": show_phase1,
                "columns": columns,
                "statuses": STATUSES,
                "total_cards": total_cards,
            },
        )


@router.post("/api/pipeline/status", response_class=JSONResponse)
async def update_pipeline_status(request: Request):
    """Update a company's global pipeline status and log the event."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    display_name = get_display_name(request)
    body = await request.json()
    orgnr = body.get("orgnr", "").strip()
    new_status = body.get("status", "").strip()

    if not orgnr or not new_status:
        return JSONResponse({"error": "Missing orgnr or status"}, status_code=400)

    if new_status not in STATUSES:
        return JSONResponse({"error": f"Invalid status '{new_status}'"}, status_code=400)

    async for db in get_db():
        company = await db.get(Company, orgnr)
        if not company:
            return JSONResponse({"error": "Company not found"}, status_code=404)

        old_status = company.pipeline_status or "unreviewed"
        if old_status == new_status:
            return JSONResponse({"ok": True, "orgnr": orgnr, "status": new_status, "unchanged": True})

        company.pipeline_status = new_status

        event = PipelineEvent(
            company_orgnr=orgnr,
            from_status=old_status,
            to_status=new_status,
            user_name=display_name or "Team",
        )
        db.add(event)
        await db.commit()

        return JSONResponse({"ok": True, "orgnr": orgnr, "status": new_status})
