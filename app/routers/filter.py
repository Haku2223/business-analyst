"""GET/POST /filter — Phase 1 filter configuration page."""

import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth import check_auth_redirect, get_display_name
from app.database import get_db
from app.models import FilterPreset
from app.services.phase1 import DEFAULT_FILTER_CONFIG

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Session key for active filter config (stored in a simple global dict per process)
# In production with multiple workers you'd use a DB-backed session, but for a
# single-team tool this is sufficient.
_ACTIVE_FILTER_CONFIG: dict = {}


def get_active_config() -> dict:
    """Return the current active filter configuration, falling back to defaults."""
    base = DEFAULT_FILTER_CONFIG.copy()
    # Ensure nested filter_types dict is also merged so old configs get new keys
    base["filter_types"] = DEFAULT_FILTER_CONFIG["filter_types"].copy()
    if _ACTIVE_FILTER_CONFIG:
        stored_types = _ACTIVE_FILTER_CONFIG.get("filter_types", {})
        base.update(_ACTIVE_FILTER_CONFIG)
        base["filter_types"] = {**DEFAULT_FILTER_CONFIG["filter_types"], **stored_types}
    return base


def set_active_config(cfg: dict) -> None:
    """Persist the active filter configuration in process memory."""
    global _ACTIVE_FILTER_CONFIG
    _ACTIVE_FILTER_CONFIG.clear()
    _ACTIVE_FILTER_CONFIG.update(cfg)


def _parse_form(form_data: dict) -> dict:
    """Parse raw form fields into a typed filter config dict."""
    def _bool(key: str) -> bool:
        return key in form_data

    def _int(key: str, default: int) -> int:
        try:
            return int(form_data.get(key, default))
        except (ValueError, TypeError):
            return default

    def _float(key: str, default: float) -> float:
        try:
            return float(form_data.get(key, default))
        except (ValueError, TypeError):
            return default

    def _str(key: str, default: str) -> str:
        return str(form_data.get(key, default)).strip()

    # SNI codes from textarea (comma or newline separated)
    sni_raw = form_data.get("sni_codes_text", "")
    sni_codes = [
        c.strip()
        for c in sni_raw.replace("\r", "").replace("\n", ",").split(",")
        if c.strip()
    ]

    # County values
    county_raw = form_data.get("soft_county_values_text", "")
    county_values = [c.strip() for c in county_raw.split(",") if c.strip()]

    # Per-filter type overrides
    def _type(filter_name: str, default: str) -> str:
        val = str(form_data.get(f"filter_type_{filter_name}", default)).strip()
        return val if val in ("hard", "soft") else default

    filter_types = {
        "company_type": _type("company_type", "hard"),
        "company_age": _type("company_age", "hard"),
        "revenue": _type("revenue", "hard"),
        "employees": _type("employees", "hard"),
        "sni_code": _type("sni_code", "hard"),
        "profitability": _type("profitability", "hard"),
        "exclude_publikt_aktiebolag": _type("exclude_publikt_aktiebolag", "hard"),
        "profit_margin": _type("profit_margin", "soft"),
        "soliditet": _type("soliditet", "soft"),
        "data_recency": _type("data_recency", "soft"),
        "county": _type("county", "soft"),
    }

    revenue_min = _int("hard_revenue_min", 3_000_000)
    revenue_max = _int("hard_revenue_max", 30_000_000)
    employees_min = _int("hard_employees_min", 3)
    employees_max = _int("hard_employees_max", 30)
    min_age = _int("hard_age_min_years", 15)

    return {
        "hard_company_type_enabled": _bool("hard_company_type_enabled"),
        "hard_company_type_value": _str("hard_company_type_value", "Aktiebolag"),
        "hard_age_enabled": _bool("hard_age_enabled"),
        "hard_age_min_years": min_age,
        "hard_revenue_enabled": _bool("hard_revenue_enabled"),
        "hard_revenue_min": revenue_min,
        "hard_revenue_max": revenue_max,
        "hard_employees_enabled": _bool("hard_employees_enabled"),
        "hard_employees_min": employees_min,
        "hard_employees_max": employees_max,
        "hard_sni_enabled": _bool("hard_sni_enabled"),
        "sni_codes": sni_codes,
        "hard_profitability_enabled": _bool("hard_profitability_enabled"),
        "hard_exclude_publikt_aktiebolag_enabled": _bool("hard_exclude_publikt_aktiebolag_enabled"),
        "soft_margin_enabled": _bool("soft_margin_enabled"),
        "soft_margin_min_pct": _float("soft_margin_min_pct", 10.0),
        "soft_soliditet_enabled": _bool("soft_soliditet_enabled"),
        "soft_soliditet_min_pct": _float("soft_soliditet_min_pct", 50.0),
        "soft_recency_enabled": _bool("soft_recency_enabled"),
        "soft_recency_months": _int("soft_recency_months", 18),
        "soft_county_enabled": _bool("soft_county_enabled"),
        "soft_county_values": county_values,
        "filter_types": filter_types,
        # Display helpers
        "revenue_min_msek": revenue_min // 1_000_000,
        "revenue_max_msek": revenue_max // 1_000_000,
        "employees_min": employees_min,
        "employees_max": employees_max,
        "min_age_years": min_age,
    }


@router.get("/filter", response_class=HTMLResponse)
async def filter_page(request: Request, load_preset: int = None):
    """Render filter configuration page."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)
    cfg = get_active_config()

    async for db in get_db():
        # Load preset if requested
        if load_preset:
            preset = await db.get(FilterPreset, load_preset)
            if preset:
                cfg = DEFAULT_FILTER_CONFIG.copy()
                cfg.update(preset.config_json)
                set_active_config(cfg)

        # Load all presets for the dropdown
        result = await db.execute(select(FilterPreset).order_by(FilterPreset.created_at.desc()))
        presets = result.scalars().all()

        return templates.TemplateResponse(
            "filter.html",
            {
                "request": request,
                "display_name": display_name,
                "active_page": "filter",
                "cfg": cfg,
                "presets": presets,
                "success": None,
                "error": None,
            },
        )


@router.post("/filter", response_class=HTMLResponse)
async def filter_post(request: Request):
    """Handle filter form submission."""
    if not check_auth_redirect(request):
        return RedirectResponse("/login", status_code=302)

    display_name = get_display_name(request)
    form_data = await request.form()
    form_dict = dict(form_data)

    action = form_dict.get("action", "save")
    cfg = _parse_form(form_dict)
    set_active_config(cfg)

    async for db in get_db():
        presets_result = await db.execute(
            select(FilterPreset).order_by(FilterPreset.created_at.desc())
        )
        presets = presets_result.scalars().all()

        if action == "save_preset":
            preset_name = str(form_dict.get("preset_name", "")).strip()
            if not preset_name:
                return templates.TemplateResponse(
                    "filter.html",
                    {
                        "request": request,
                        "display_name": display_name,
                        "active_page": "filter",
                        "cfg": cfg,
                        "presets": presets,
                        "success": None,
                        "error": "Preset name cannot be empty.",
                    },
                )
            preset = FilterPreset(
                name=preset_name,
                config_json=cfg,
                created_by=display_name,
            )
            db.add(preset)
            await db.commit()

            # Reload presets
            presets_result = await db.execute(
                select(FilterPreset).order_by(FilterPreset.created_at.desc())
            )
            presets = presets_result.scalars().all()

            return templates.TemplateResponse(
                "filter.html",
                {
                    "request": request,
                    "display_name": display_name,
                    "active_page": "filter",
                    "cfg": cfg,
                    "presets": presets,
                    "success": f"Preset '{preset_name}' saved.",
                    "error": None,
                },
            )

        # Default: just save config
        return templates.TemplateResponse(
            "filter.html",
            {
                "request": request,
                "display_name": display_name,
                "active_page": "filter",
                "cfg": cfg,
                "presets": presets,
                "success": "Filter configuration saved.",
                "error": None,
            },
        )
