"""Authentication: bcrypt password check, JWT issue/verify, display name cookie.

Uses a pure-Python HS256 JWT implementation (stdlib hmac + hashlib) to avoid
dependencies on the cryptography / cffi packages.
"""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request, status
from passlib.context import CryptContext

from app.config import get_settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against its bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


def hash_password(plain_password: str) -> str:
    """Hash a plain password with bcrypt."""
    return pwd_context.hash(plain_password)


# ---------------------------------------------------------------------------
# Pure-Python HS256 JWT (no cryptography dependency)
# ---------------------------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    # Re-add padding
    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _jwt_sign(payload: dict, secret: str) -> str:
    """Encode a JWT with HS256."""
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url_encode(json.dumps(payload, default=str, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url_encode(sig)}"


def _jwt_verify(token: str, secret: str) -> Optional[dict]:
    """Decode and verify a JWT. Returns payload or None."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        h, p, sig = parts
        signing_input = f"{h}.{p}".encode("ascii")
        expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        provided_sig = _b64url_decode(sig)
        if not hmac.compare_digest(expected_sig, provided_sig):
            return None
        payload = json.loads(_b64url_decode(p).decode("utf-8"))
        # Check expiry
        exp = payload.get("exp")
        if exp:
            # exp might be a float/int (epoch) or ISO string
            if isinstance(exp, (int, float)):
                exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
            else:
                exp_dt = datetime.fromisoformat(str(exp))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp_dt:
                return None
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def create_access_token(data: dict) -> str:
    """Create a JWT access token with 30-day expiry."""
    settings = get_settings()
    payload = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.jwt_expiry_days)
    payload["exp"] = expire.timestamp()
    return _jwt_sign(payload, settings.jwt_secret)


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT. Returns payload dict or None."""
    settings = get_settings()
    return _jwt_verify(token, settings.jwt_secret)


# ---------------------------------------------------------------------------
# FastAPI dependency: require authenticated user
# ---------------------------------------------------------------------------


def get_token_from_request(request: Request) -> Optional[str]:
    """Extract JWT from cookie or Authorization header."""
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    return token


def get_current_user(request: Request) -> dict:
    """
    FastAPI dependency: return the current user payload or raise 401.
    For HTML pages use require_auth instead (redirects to /login).
    """
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    return payload


def get_display_name(request: Request) -> str:
    """Extract display name from cookie, defaulting to 'Team'."""
    return request.cookies.get("display_name", "Team")


def require_auth(request: Request) -> Optional[dict]:
    """
    FastAPI dependency for HTML pages: redirects to /login if not authenticated.
    Use Depends(require_auth) on all page routes.
    """
    token = get_token_from_request(request)
    if not token:
        return None
    return decode_access_token(token)


def check_auth_redirect(request: Request) -> Optional[dict]:
    """
    Check auth and return payload, or None if not authenticated.
    Routers should redirect to /login if this returns None.
    """
    token = get_token_from_request(request)
    if not token:
        return None
    return decode_access_token(token)
