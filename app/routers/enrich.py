"""GET/POST /enrich — Phase 2 control panel."""

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth import check_auth_redirect, get_display_name
from app.database import get_db
from app.models import Batch, BatchCompany, Company, Phase2Job
from app.services.phase2a import (
    cancel_job,
    get_bokslut_url,
    pause_job,
    resume_job,
    run_phase2a_job,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/enrich", response_class=HTMLResponse)
async def enrich_page(request: Request):
    """Phase 2 enrichment page."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)

    async for db in get_db():
        # Load batches
        batches_result = await db.execute(
            select(Batch).order_by(Batch.upload_timestamp.desc())
        )
        all_batches = batches_result.scalars().all()

        # Load active/recent jobs
        jobs_result = await db.execute(
            select(Phase2Job).order_by(Phase2Job.id.desc()).limit(10)
        )
        jobs = jobs_result.scalars().all()

        # Count Phase 1 passed companies for the most recent batch
        eligible_count = 0
        batch_id = None
        if all_batches:
            batch_id = all_batches[0].id
            count_result = await db.execute(
                select(BatchCompany)
                .where(
                    BatchCompany.batch_id == batch_id,
                    BatchCompany.phase1_passed == True,
                )
            )
            eligible_count = len(count_result.scalars().all())

        return templates.TemplateResponse(
            "enrich.html",
            {
                "request": request,
                "display_name": display_name,
                "active_page": "enrich",
                "batches": all_batches,
                "jobs": jobs,
                "eligible_count": eligible_count,
                "current_batch_id": batch_id,
            },
        )


@router.post("/api/enrich/start", response_class=JSONResponse)
async def start_enrichment(request: Request, background_tasks: BackgroundTasks):
    """Start a Phase 2a enrichment job."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    batch_id = body.get("batch_id")
    if not batch_id:
        return JSONResponse({"error": "batch_id required"}, status_code=400)

    async for db in get_db():
        # Get Phase 1 passed companies for this batch
        query = (
            select(BatchCompany.company_orgnr)
            .where(
                BatchCompany.batch_id == batch_id,
                BatchCompany.phase1_passed == True,
            )
        )
        result = await db.execute(query)
        orgnrs = [r[0] for r in result.all()]

        if not orgnrs:
            return JSONResponse({"error": "No eligible companies"}, status_code=400)

        # Create job record
        job = Phase2Job(
            batch_id=batch_id,
            status="pending",
            companies_total=len(orgnrs),
            companies_done=0,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)

        # Estimate time: ~0.5s per company + overhead
        est_minutes = round(len(orgnrs) * 0.6 / 60, 1)

        # Run in background
        background_tasks.add_task(run_phase2a_job, job.id, orgnrs)

        return JSONResponse({
            "ok": True,
            "job_id": job.id,
            "companies": len(orgnrs),
            "estimated_minutes": est_minutes,
        })


@router.post("/api/enrich/pause/{job_id}", response_class=JSONResponse)
async def pause_enrichment(request: Request, job_id: int):
    """Pause a running Phase 2a job."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    pause_job(job_id)
    return JSONResponse({"ok": True, "job_id": job_id, "action": "paused"})


@router.post("/api/enrich/resume/{job_id}", response_class=JSONResponse)
async def resume_enrichment(request: Request, job_id: int):
    """Resume a paused Phase 2a job."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    resume_job(job_id)
    return JSONResponse({"ok": True, "job_id": job_id, "action": "resumed"})


@router.post("/api/enrich/cancel/{job_id}", response_class=JSONResponse)
async def cancel_enrichment(request: Request, job_id: int):
    """Cancel a running Phase 2a job."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    cancel_job(job_id)
    return JSONResponse({"ok": True, "job_id": job_id, "action": "cancelled"})


@router.get("/api/enrich/status/{job_id}", response_class=JSONResponse)
async def job_status(request: Request, job_id: int):
    """Get current status of a Phase 2a job."""
    if not check_auth_redirect(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    async for db in get_db():
        job = await db.get(Phase2Job, job_id)
        if not job:
            return JSONResponse({"error": "Job not found"}, status_code=404)

        return JSONResponse({
            "job_id": job.id,
            "status": job.status,
            "companies_total": job.companies_total,
            "companies_done": job.companies_done,
            "errors": job.errors_json or [],
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        })
