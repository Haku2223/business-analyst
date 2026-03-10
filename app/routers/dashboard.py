"""GET / — Dashboard page."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.auth import check_auth_redirect, get_display_name
from app.database import get_db
from app.models import Batch, BatchCompany, Company, PipelineEvent

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Dashboard: pipeline summary stats, recent activity, latest batch info."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)

    async for db in get_db():
        # Pipeline stats
        result = await db.execute(select(Company))
        companies = result.scalars().all()

        total = len(companies)
        watch = sum(1 for c in companies if c.pipeline_status == "watch")
        deep_dive = sum(1 for c in companies if c.pipeline_status == "deep_dive")
        passed = sum(1 for c in companies if c.pipeline_status == "pass")

        # Phase 1 passed count via batch_companies
        bc_result = await db.execute(
            select(func.count()).where(BatchCompany.phase1_passed == True)  # noqa: E712
        )
        phase1_passed = bc_result.scalar() or 0

        stats = {
            "total": total,
            "watch": watch,
            "deep_dive": deep_dive,
            "passed": passed,
            "phase1_passed": phase1_passed,
        }

        # Deep Dive companies (up to 10)
        deep_dive_companies = [c for c in companies if c.pipeline_status == "deep_dive"][:10]

        # Recent pipeline events (up to 15)
        ev_result = await db.execute(
            select(PipelineEvent).order_by(PipelineEvent.timestamp.desc()).limit(15)
        )
        recent_events = ev_result.scalars().all()

        # Latest batch
        batch_result = await db.execute(
            select(Batch).order_by(Batch.upload_timestamp.desc()).limit(1)
        )
        latest_batch = batch_result.scalar_one_or_none()

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "display_name": display_name,
                "active_page": "dashboard",
                "stats": stats,
                "deep_dive_companies": deep_dive_companies,
                "recent_events": recent_events,
                "latest_batch": latest_batch,
            },
        )
