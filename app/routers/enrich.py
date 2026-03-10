"""GET /enrich — Phase 2 control panel (stub for Phase 1 build)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import check_auth_redirect, get_display_name

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/enrich", response_class=HTMLResponse)
async def enrich_page(request: Request):
    """Phase 2 enrichment page (stub)."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)
    return templates.TemplateResponse(
        "enrich.html",
        {"request": request, "display_name": display_name, "active_page": "enrich"},
    )
