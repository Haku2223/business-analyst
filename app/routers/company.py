"""GET /company/{orgnr} — Company detail page."""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth import check_auth_redirect, get_display_name
from app.database import get_db
from app.models import Company, Note, PipelineEvent

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/company/{orgnr}", response_class=HTMLResponse)
async def company_detail(request: Request, orgnr: str):
    """Company detail page."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)

    async for db in get_db():
        company = await db.get(Company, orgnr)

        notes = []
        events = []
        if company:
            notes_result = await db.execute(
                select(Note)
                .where(Note.company_orgnr == orgnr)
                .order_by(Note.created_at.desc())
            )
            notes = notes_result.scalars().all()

            events_result = await db.execute(
                select(PipelineEvent)
                .where(PipelineEvent.company_orgnr == orgnr)
                .order_by(PipelineEvent.timestamp.desc())
            )
            events = events_result.scalars().all()

        # Prepare historical financials for template
        historical_raw = company.historical_financials if company else None
        # historical_financials may be stored as {"years": [...], "phase2a_result": {...}}
        # or as a plain list — normalize to a list of year dicts
        if isinstance(historical_raw, dict):
            historical = historical_raw.get("years", [])
        elif isinstance(historical_raw, list):
            historical = historical_raw
        else:
            historical = []

        phase2a_results = None
        # Check phase2a results inside historical_financials dict
        if isinstance(historical_raw, dict) and "phase2a_result" in historical_raw:
            p2a = historical_raw["phase2a_result"]
            phase2a_results = {
                "passed": p2a.get("passed"),
                "hard_fails": p2a.get("hard_failed", []),
                "soft_fails": p2a.get("soft_failed", []),
            }
        # Also check extra_data for backwards compatibility
        if not phase2a_results and company and company.extra_data:
            ed = company.extra_data
            if "phase2a_passed" in ed:
                phase2a_results = {
                    "passed": ed.get("phase2a_passed"),
                    "hard_fails": ed.get("phase2a_hard_fails", []),
                    "soft_fails": ed.get("phase2a_soft_fails", []),
                }

        # Build display_financial: Phase 2 multi-year if available, otherwise Phase 1 single year.
        # is_phase2 flag lets the template know which mode it's in.
        phase1_data = None
        display_financial: list = []
        is_phase2 = bool(historical)
        if company:
            ex = company.extra_data or {}
            phase1_data = {
                "slutdatum": company.bokslutsperiod_slut,
                # Löner & Utdelning
                "loner_styrelse_vd": company.loner_styrelse_vd,
                "loner_ovriga": ex.get("loner_ovriga"),
                "foreslagen_utdelning": ex.get("foreslagen_utdelning"),
                # Resultaträkning
                "nettoomsattning": ex.get("nettoomsattning"),
                "ovrig_omsattning": ex.get("ovrig_omsattning"),
                "omsattning": company.omsattning,
                "lagerforandring": ex.get("lagerforandringar"),
                "rorelsekostnader": ex.get("rorelsekostnader"),
                "rorelseresultat": company.rorelsresultat,
                "finansiella_intakter": ex.get("finansiella_intakter"),
                "finansiella_kostnader": ex.get("finansiella_kostnader"),
                "ovriga_finansiella_kostnader": ex.get("ovriga_finansiella_kostnader"),
                "resultat_efter_finansnetto": company.resultat_efter_finansnetto,
                "resultat_fore_skatt": company.resultat_fore_skatt,
                "skatt": ex.get("skatt_pa_arets_resultat"),
                "arets_resultat": company.arets_resultat,
                # Balansräkning — assets
                "immateriella_anlaggningstillgangar": ex.get("immateriella_anlaggningstillgangar"),
                "materiella_anlaggningstillgangar": ex.get("materiella_anlaggningstillgangar"),
                "finansiella_anlaggningstillgangar": ex.get("finansiella_anlaggningstillgangar"),
                "anlaggningstillgangar": ex.get("anlaggningstillgangar"),
                "varulager": ex.get("varulager"),
                "kundfordringar": ex.get("kundfordringar"),
                "kassa_och_bank": company.kassa_och_bank,
                "omsattningstillgangar": ex.get("omsattningstillgangar"),
                "summa_tillgangar": company.summa_tillgangar,
                # Balansräkning — equity & liabilities
                "fritt_eget_kapital": ex.get("fritt_eget_kapital"),
                "obeskattade_reserver": ex.get("obeskattade_reserver"),
                "eget_kapital": company.eget_kapital,
                "avsattningar": ex.get("avsattningar"),
                "langfristiga_skulder": ex.get("langfristiga_skulder"),
                "leverantorsskulder": ex.get("leverantorsskulder"),
                "kortfristiga_skulder": ex.get("kortfristiga_skulder"),
                "summa_eget_kapital_och_skulder": ex.get("summa_eget_kapital_och_skulder"),
                # Nyckeltal
                "vinstmarginal_pct": company.vinstmarginal,
                "soliditet_pct": company.soliditet,
                "kassalikviditet_pct": company.kassalikviditet,
                "skuldsattningsgrad": company.skuldsattningsgrad,
                "avkastning_eget_kapital_pct": ex.get("avkastning_eget_kapital"),
                "avkastning_totalt_kapital_pct": ex.get("avkastning_totalt_kapital"),
            }
            display_financial = historical if historical else [phase1_data]

        return templates.TemplateResponse(
            "company.html",
            {
                "request": request,
                "display_name": display_name,
                "active_page": None,
                "company": company,
                "notes": notes,
                "events": events,
                "historical": historical or [],
                "phase2a_results": phase2a_results,
                "phase1_data": phase1_data,
                "display_financial": display_financial,
                "is_phase2": is_phase2,
            },
        )


@router.post("/company/{orgnr}/note")
async def add_note(request: Request, orgnr: str, note_text: str = Form(...)):
    """Add a note to a company."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)

    async for db in get_db():
        note = Note(
            company_orgnr=orgnr,
            note_text=note_text.strip(),
            user_name=display_name,
        )
        db.add(note)
        await db.commit()

    return RedirectResponse(f"/company/{orgnr}", status_code=302)


@router.post("/api/company/{orgnr}/status", response_class=JSONResponse)
async def update_status(request: Request, orgnr: str):
    """AJAX: update pipeline status for a company."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    display_name = get_display_name(request)

    body = await request.json()
    new_status = str(body.get("status", "")).strip()
    valid_statuses = {"unreviewed", "watch", "deep_dive", "pass"}
    if new_status not in valid_statuses:
        return JSONResponse({"error": "Invalid status"}, status_code=400)

    async for db in get_db():
        company = await db.get(Company, orgnr)
        if not company:
            return JSONResponse({"error": "Company not found"}, status_code=404)

        old_status = company.pipeline_status
        company.pipeline_status = new_status

        # Log the event
        event = PipelineEvent(
            company_orgnr=orgnr,
            from_status=old_status,
            to_status=new_status,
            user_name=display_name,
        )
        db.add(event)
        await db.commit()

    return JSONResponse({"ok": True, "orgnr": orgnr, "status": new_status})
