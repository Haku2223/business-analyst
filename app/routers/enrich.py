"""GET/POST /enrich — Phase 2 control panel.

Provides the UI and API endpoints for running Phase 2a (historical financial
scraping from Allabolag /bokslut) and monitoring job progress.
"""

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.auth import check_auth_redirect, get_display_name
from app.database import get_db
from app.models import Batch, BatchCompany, Company, Phase2Job
from app.services.phase2a import (
    DEFAULT_PHASE2A_CONFIG,
    get_job_status,
    pause_job,
    resume_job,
    start_job,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/enrich", response_class=HTMLResponse)
async def enrich_page(request: Request, batch_id: int = None):
    """Phase 2 enrichment control panel."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)

    async for db in get_db():
        # Load all batches for selection
        batches_result = await db.execute(
            select(Batch).order_by(Batch.upload_timestamp.desc())
        )
        all_batches = batches_result.scalars().all()

        # Use specified batch_id or fall back to most recent
        if batch_id is None and all_batches:
            batch_id = all_batches[0].id

        # Count Phase 1 survivors for the current batch
        phase1_count = 0
        if batch_id:
            count_result = await db.execute(
                select(func.count())
                .select_from(BatchCompany)
                .where(
                    BatchCompany.batch_id == batch_id,
                    BatchCompany.phase1_passed == True,  # noqa: E712
                )
            )
            phase1_count = count_result.scalar() or 0

        # Load existing Phase 2 jobs for this batch
        jobs = []
        if batch_id:
            jobs_result = await db.execute(
                select(Phase2Job)
                .where(Phase2Job.batch_id == batch_id)
                .order_by(Phase2Job.id.desc())
            )
            jobs = jobs_result.scalars().all()

        # Get enrichment status summary
        enriched_count = 0
        pending_count = 0
        failed_count = 0
        if batch_id:
            # Count companies by phase2 status
            status_result = await db.execute(
                select(Company.phase2_status, func.count())
                .join(
                    BatchCompany,
                    BatchCompany.company_orgnr == Company.orgnr,
                )
                .where(
                    BatchCompany.batch_id == batch_id,
                    BatchCompany.phase1_passed == True,  # noqa: E712
                )
                .group_by(Company.phase2_status)
            )
            for status, count in status_result.all():
                if status == "complete":
                    enriched_count = count
                elif status == "failed":
                    failed_count = count
                else:
                    pending_count += count

        # Estimate time for pending companies
        est_minutes = round(pending_count * 0.5 / 60, 1) if pending_count > 0 else 0

        return templates.TemplateResponse(
            "enrich.html",
            {
                "request": request,
                "display_name": display_name,
                "active_page": "enrich",
                "all_batches": all_batches,
                "batch_id": batch_id,
                "phase1_count": phase1_count,
                "enriched_count": enriched_count,
                "pending_count": pending_count,
                "failed_count": failed_count,
                "est_minutes": est_minutes,
                "jobs": jobs,
                "config": DEFAULT_PHASE2A_CONFIG,
            },
        )


@router.post("/api/enrich/start", response_class=JSONResponse)
async def api_start_enrichment(request: Request):
    """Start a Phase 2a enrichment job for a batch."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    batch_id = body.get("batch_id")
    if not batch_id:
        return JSONResponse({"error": "batch_id is required"}, status_code=400)

    # Merge user-provided config with defaults
    config = DEFAULT_PHASE2A_CONFIG.copy()
    user_config = body.get("config", {})
    if user_config:
        config.update(user_config)

    async for db in get_db():
        batch = await db.get(Batch, batch_id)
        if not batch:
            return JSONResponse({"error": "Batch not found"}, status_code=404)

        # Check for already-running jobs for this batch
        running_result = await db.execute(
            select(Phase2Job).where(
                Phase2Job.batch_id == batch_id,
                Phase2Job.status.in_(["pending", "running"]),
            )
        )
        running_jobs = running_result.scalars().all()
        if running_jobs:
            return JSONResponse(
                {"error": "A job is already running for this batch"},
                status_code=409,
            )

        # Get Phase 1 survivors
        result = await db.execute(
            select(BatchCompany.company_orgnr)
            .where(
                BatchCompany.batch_id == batch_id,
                BatchCompany.phase1_passed == True,  # noqa: E712
            )
            .order_by(BatchCompany.id.asc())
        )
        orgnrs = [r[0] for r in result.all()]

        if not orgnrs:
            return JSONResponse(
                {"error": "No Phase 1 survivors to enrich"},
                status_code=400,
            )

        # Filter out already-enriched companies unless re-run requested
        rerun = body.get("rerun", False)
        if not rerun:
            enriched_result = await db.execute(
                select(Company.orgnr).where(
                    Company.orgnr.in_(orgnrs),
                    Company.phase2_status == "complete",
                )
            )
            already_done = {r[0] for r in enriched_result.all()}
            orgnrs = [o for o in orgnrs if o not in already_done]

        if not orgnrs:
            return JSONResponse(
                {"error": "All companies already enriched. Use rerun=true to re-process."},
                status_code=400,
            )

        est_minutes = round(len(orgnrs) * 0.5 / 60, 1)
        job_id = await start_job(batch_id, orgnrs, config)

        return JSONResponse({
            "ok": True,
            "job_id": job_id,
            "companies_count": len(orgnrs),
            "estimated_minutes": est_minutes,
        })


@router.post("/api/enrich/pause", response_class=JSONResponse)
async def api_pause_job(request: Request):
    """Pause a running Phase 2a job."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    job_id = body.get("job_id")
    if not job_id:
        return JSONResponse({"error": "job_id is required"}, status_code=400)

    success = await pause_job(int(job_id))
    if success:
        return JSONResponse({"ok": True, "status": "paused"})
    return JSONResponse({"error": "Job not found or not running"}, status_code=404)


@router.post("/api/enrich/resume", response_class=JSONResponse)
async def api_resume_job(request: Request):
    """Resume a paused Phase 2a job."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    job_id = body.get("job_id")
    if not job_id:
        return JSONResponse({"error": "job_id is required"}, status_code=400)

    success = await resume_job(int(job_id))
    if success:
        return JSONResponse({"ok": True, "status": "running"})
    return JSONResponse({"error": "Job not found or cannot be resumed"}, status_code=404)


@router.get("/api/enrich/status/{job_id}", response_class=JSONResponse)
async def api_job_status(request: Request, job_id: int):
    """Get the current status and progress of a Phase 2a job."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    status = await get_job_status(job_id)
    if status is None:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    return JSONResponse(status)


@router.get("/api/enrich/batch/{batch_id}/summary", response_class=JSONResponse)
async def api_batch_enrichment_summary(request: Request, batch_id: int):
    """Get enrichment summary for a batch (counts by phase2 status)."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    async for db in get_db():
        result = await db.execute(
            select(Company.phase2_status, func.count())
            .join(
                BatchCompany,
                BatchCompany.company_orgnr == Company.orgnr,
            )
            .where(
                BatchCompany.batch_id == batch_id,
                BatchCompany.phase1_passed == True,  # noqa: E712
            )
            .group_by(Company.phase2_status)
        )
        counts: dict[str, int] = {}
        for status, count in result.all():
            counts[status] = count

        # Get the latest job for this batch
        job_result = await db.execute(
            select(Phase2Job)
            .where(Phase2Job.batch_id == batch_id)
            .order_by(Phase2Job.id.desc())
            .limit(1)
        )
        latest_job = job_result.scalar_one_or_none()

        return JSONResponse({
            "batch_id": batch_id,
            "counts": counts,
            "total_phase1": sum(counts.values()),
            "latest_job": {
                "id": latest_job.id,
                "status": latest_job.status,
                "companies_done": latest_job.companies_done,
                "companies_total": latest_job.companies_total,
            } if latest_job else None,
        })
