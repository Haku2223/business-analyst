"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import init_db

# Routers
from app.routers.auth_router import router as auth_router
from app.routers.dashboard import router as dashboard_router
from app.routers.upload import router as upload_router
from app.routers.filter import router as filter_router
from app.routers.results import router as results_router
from app.routers.enrich import router as enrich_router
from app.routers.pipeline import router as pipeline_router
from app.routers.company import router as company_router
from app.routers.export import router as export_router
from app.routers.settings import router as settings_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialise database on startup."""
    logger.info("Initialising database…")
    await init_db()
    logger.info("Database ready.")
    yield
    logger.info("Shutting down.")


app = FastAPI(
    title="Business Analyst Agent",
    description="Swedish business acquisition deal sourcing tool",
    version="0.1.0",
    lifespan=lifespan,
)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Include all routers
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(upload_router)
app.include_router(filter_router)
app.include_router(results_router)
app.include_router(enrich_router)
app.include_router(pipeline_router)
app.include_router(company_router)
app.include_router(export_router)
app.include_router(settings_router)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    """Return a friendly 404 page."""
    templates = Jinja2Templates(directory="app/templates")
    # Check if authenticated; if not, send to login
    from app.auth import check_auth_redirect
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(
        content=f"<h1>404 — Page not found</h1><p><a href='/'>Go to dashboard</a></p>",
        status_code=404,
    )
