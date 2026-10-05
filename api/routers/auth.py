"""
api/routers/auth.py
--------------------
Google OAuth login, logout, and session management.
Password-based login has been removed — all logins are authenticated via Google.
"""

from __future__ import annotations

import secrets
import urllib.parse
from datetime import timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.templates import templates
from api.dependencies import get_current_user, require_admin
from core.database import get_db
from core.config import settings
from core.logging_config import get_logger
from core.security import (
    create_access_token,
    decode_access_token,
    generate_csrf_token,
)
from models.user import User

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])


# ── Google OAuth Login ──────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    # If user is already authenticated with valid cookie, send directly to dashboard
    token = request.cookies.get("access_token")
    if token:
        try:
            decode_access_token(token)
            return RedirectResponse(url="/", status_code=302)
        except Exception:
            pass

    csrf = generate_csrf_token()
    response = templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "csrf_token": csrf,
            "error": error,
            "google_login_url": "/auth/google",
        },
    )
    response.set_cookie("csrf_token", csrf, httponly=False, samesite="lax")
    return response


@router.get("/google")
async def google_login(request: Request):
    """Initiate Google OAuth 2.0 flow."""
    if not settings.google_client_id or not settings.google_client_secret:
        return RedirectResponse(
            url="/auth/login?error=Google+OAuth+is+not+configured+in+.env+(missing+Client+ID+or+Secret)",
            status_code=302,
        )

    state = secrets.token_urlsafe(32)
    redirect_uri = settings.google_login_redirect_uri or f"{settings.app_base_url.rstrip('/')}/auth/google/callback"

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
        "access_type": "online",
    }
    google_auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"

    response = RedirectResponse(url=google_auth_url, status_code=302)
    response.set_cookie(
        "oauth_state",
        state,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        max_age=600,
    )
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Handle Google OAuth callback, retrieve user profile, and issue session."""
    if error:
        log.warning("Google login error returned from Google", error=error)
        return RedirectResponse(url=f"/auth/login?error=Google+login+error:+{error}", status_code=302)

    if not code:
        return RedirectResponse(url="/auth/login?error=Missing+authorization+code+from+Google", status_code=302)

    cookie_state = request.cookies.get("oauth_state")
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        log.warning("Google OAuth state mismatch", state=state, cookie_state=cookie_state)
        return RedirectResponse(
            url="/auth/login?error=Invalid+session+state.+Please+try+signing+in+again.",
            status_code=302,
        )

    redirect_uri = settings.google_login_redirect_uri or f"{settings.app_base_url.rstrip('/')}/auth/google/callback"

    # Exchange authorization code for Google access token
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            log.error("Google token exchange failed", status=token_resp.status_code, body=token_resp.text)
            return RedirectResponse(
                url="/auth/login?error=Failed+to+exchange+Google+authorization+code",
                status_code=302,
            )

        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return RedirectResponse(
                url="/auth/login?error=No+access+token+received+from+Google",
                status_code=302,
            )

        # Retrieve user profile from Google
        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo_resp.status_code != 200:
            log.error("Failed to retrieve Google userinfo", status=userinfo_resp.status_code, body=userinfo_resp.text)
            return RedirectResponse(
                url="/auth/login?error=Failed+to+retrieve+Google+user+profile",
                status_code=302,
            )

        userinfo = userinfo_resp.json()

    email = (userinfo.get("email") or "").lower().strip()
    google_id = str(userinfo.get("sub") or "")
    picture = userinfo.get("picture")

    if not email:
        return RedirectResponse(
            url="/auth/login?error=No+email+associated+with+this+Google+account",
            status_code=302,
        )

    # Email whitelist enforcement (if ALLOWED_GOOGLE_EMAILS is set)
    allowed_list = [e.strip().lower() for e in settings.allowed_google_emails.split(",") if e.strip()]
    if allowed_list and email not in allowed_list:
        log.warning("Unauthorized Google login attempt", email=email)
        return RedirectResponse(
            url=f"/auth/login?error=Access+denied:+{email}+is+not+an+authorized+administrator",
            status_code=302,
        )

    # Locate or create user in database
    result = await db.execute(select(User).where((User.email == email) | (User.username == email)))
    user = result.scalar_one_or_none()

    if not user:
        # Check if initial seeded user exists and link it
        res_admin = await db.execute(select(User).where(User.id == 1))
        initial_user = res_admin.scalar_one_or_none()
        if initial_user and (not initial_user.email or initial_user.username == "admin"):
            user = initial_user
            user.username = email
            user.email = email
            user.google_id = google_id
            user.avatar_url = picture
            user.is_active = True
        else:
            user = User(
                username=email,
                email=email,
                google_id=google_id,
                avatar_url=picture,
                hashed_password="google_oauth_no_password",
                is_active=True,
            )
            db.add(user)
    else:
        user.email = email
        user.google_id = google_id
        user.avatar_url = picture
        user.is_active = True

    await db.commit()

    # Generate session access token
    token = create_access_token(user.id, user.username)
    csrf = generate_csrf_token()

    redirect = RedirectResponse(url="/", status_code=302)
    redirect.set_cookie(
        "access_token",
        token,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        max_age=settings.session_expire_minutes * 60,
    )
    redirect.set_cookie("csrf_token", csrf, httponly=False, samesite="lax")
    redirect.delete_cookie("oauth_state")
    log.info("Google login successful", email=email, user_id=user.id)
    return redirect


# ── Legacy Form POST (Disabled) ─────────────────────────────────────────────

@router.post("/login")
async def legacy_login():
    return RedirectResponse(
        url="/auth/login?error=Password+login+has+been+disabled.+Please+use+Google+Sign-In.",
        status_code=303,
    )


# ── Logout ─────────────────────────────────────────────────────────────────

@router.post("/logout")
async def logout(response: Response):
    redirect = RedirectResponse(url="/auth/login", status_code=302)
    redirect.delete_cookie("access_token")
    redirect.delete_cookie("csrf_token")
    redirect.delete_cookie("oauth_state")
    return redirect
