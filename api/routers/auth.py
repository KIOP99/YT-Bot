"""
api/routers/auth.py
--------------------
Login, logout, TOTP setup, and session management.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.templates import templates
from api.dependencies import get_current_user, require_admin
from api.middleware import limiter
from core.database import get_db
from core.config import settings
from core.logging_config import get_logger
from core.security import (
    create_access_token,
    generate_csrf_token,
    hash_password,
    verify_password,
    verify_totp,
    generate_totp_secret,
    get_totp_uri,
)
from models.user import User

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])


# ── Login ──────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    csrf = generate_csrf_token()
    response = templates.TemplateResponse(
        "login.html",
        {"request": request, "csrf_token": csrf, "error": None},
    )
    response.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return response


@router.post("/login")
@limiter.limit("5/minute")
async def login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    totp_code: Optional[str] = Form(default=None),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.username == username))
    user: Optional[User] = result.scalar_one_or_none()

    if not user or not verify_password(password, user.hashed_password):
        log.warning("Failed login attempt", username=username, ip=request.client.host)
        csrf = generate_csrf_token()
        resp = templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "csrf_token": csrf,
                "error": "Invalid username or password.",
            },
            status_code=401,
        )
        resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
        return resp

    if user.totp_enabled:
        if not totp_code or not verify_totp(user.totp_secret, totp_code):
            csrf = generate_csrf_token()
            resp = templates.TemplateResponse(
                "login.html",
                {
                    "request": request,
                    "csrf_token": csrf,
                    "error": "Invalid 2FA code.",
                    "need_totp": True,
                },
                status_code=401,
            )
            resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
            return resp

    token = create_access_token(user.id, user.username)
    csrf = generate_csrf_token()

    redirect = RedirectResponse(url="/", status_code=302)
    redirect.set_cookie(
        "access_token",
        token,
        httponly=True,
        samesite="strict",
        secure=settings.is_production,
        max_age=settings.session_expire_minutes * 60,
    )
    redirect.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    log.info("Login successful", user_id=user.id, username=user.username)
    return redirect


# ── Logout ─────────────────────────────────────────────────────────────────

@router.post("/logout")
async def logout(response: Response):
    redirect = RedirectResponse(url="/auth/login", status_code=302)
    redirect.delete_cookie("access_token")
    redirect.delete_cookie("csrf_token")
    return redirect


# ── TOTP Setup ─────────────────────────────────────────────────────────────

@router.get("/totp/setup", response_class=HTMLResponse)
async def totp_setup_page(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    secret = generate_totp_secret()
    uri = get_totp_uri(secret, user.username)
    csrf = generate_csrf_token()
    resp = templates.TemplateResponse(
        "security.html",
        {
            "request": request,
            "user": user,
            "totp_uri": uri,
            "totp_secret": secret,
            "csrf_token": csrf,
        },
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    resp.set_cookie("pending_totp_secret", secret, httponly=True, samesite="strict", max_age=300)
    return resp


@router.post("/totp/enable")
async def totp_enable(
    request: Request,
    code: str = Form(...),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    secret = request.cookies.get("pending_totp_secret")
    if not secret or not verify_totp(secret, code):
        raise HTTPException(status_code=400, detail="Invalid TOTP code")

    user.totp_secret = secret
    user.totp_enabled = True
    await db.flush()

    redirect = RedirectResponse(url="/security?msg=2fa_enabled", status_code=302)
    redirect.delete_cookie("pending_totp_secret")
    return redirect


@router.post("/totp/disable")
async def totp_disable(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user.totp_enabled = False
    user.totp_secret = None
    await db.flush()
    return RedirectResponse(url="/security?msg=2fa_disabled", status_code=302)
