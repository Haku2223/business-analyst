"""Login / logout routes."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import check_auth_redirect, create_access_token, verify_password
from app.config import get_settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Render login page. Redirect to dashboard if already authenticated."""
    if check_auth_redirect(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    password: str = Form(...),
    display_name: str = Form(""),
):
    """Validate team password and set JWT cookie."""
    settings = get_settings()

    # If no password hash configured, allow any login in dev mode
    if not settings.team_password_hash:
        # Dev mode: accept any non-empty password
        if not password:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Password required."},
                status_code=400,
            )
    else:
        if not verify_password(password, settings.team_password_hash):
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Incorrect password. Please try again."},
                status_code=401,
            )

    token = create_access_token({"sub": "team"})
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        "access_token",
        token,
        max_age=60 * 60 * 24 * 30,  # 30 days
        httponly=True,
        samesite="lax",
    )
    if display_name.strip():
        response.set_cookie(
            "display_name",
            display_name.strip()[:50],
            max_age=60 * 60 * 24 * 365,
            httponly=False,
            samesite="lax",
        )
    return response


@router.get("/logout")
async def logout():
    """Clear JWT cookie and redirect to login."""
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("access_token")
    return response
