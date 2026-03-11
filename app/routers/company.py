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

        return templates.TemplateResponse(
            "company.html",
            {
                "request": request,
                "display_name": display_name,
                "active_page": None,
                "company": company,
                "notes": notes,
                "events": events,
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
