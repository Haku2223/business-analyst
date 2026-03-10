"""GET /pipeline — Kanban board (stub for Phase 1 build)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import check_auth_redirect, get_display_name

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/pipeline", response_class=HTMLResponse)
async def pipeline_page(request: Request):
    """Kanban board page (stub)."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)
    return templates.TemplateResponse(
        "pipeline.html",
        {"request": request, "display_name": display_name, "active_page": "pipeline"},
    )
