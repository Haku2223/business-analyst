"""GET/POST /settings — Application settings page."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth import check_auth_redirect, get_display_name
from app.config import get_settings
from app.database import get_db
from app.models import FilterPreset

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)
    settings = get_settings()

    async for db in get_db():
        result = await db.execute(
            select(FilterPreset).order_by(FilterPreset.created_at.desc())
        )
        presets = result.scalars().all()

        return templates.TemplateResponse(
            "settings.html",
            {
                "request": request,
                "display_name": display_name,
                "active_page": "settings",
                "presets": presets,
                "db_type": "SQLite" if settings.is_sqlite else "PostgreSQL",
                "has_api_key": bool(settings.anthropic_api_key),
                "success": None,
            },
        )


@router.post("/settings/delete-preset/{preset_id}")
async def delete_preset(request: Request, preset_id: int):
    """Delete a filter preset."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    async for db in get_db():
        preset = await db.get(FilterPreset, preset_id)
        if preset:
            await db.delete(preset)
            await db.commit()

    return RedirectResponse("/settings", status_code=302)
