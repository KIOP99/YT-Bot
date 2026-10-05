"""
api/routers/security.py
------------------------
Security settings: password change, credential rotation trigger, logs view.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.templates import templates
from api.dependencies import require_admin
from core.database import get_db
from core.logging_config import get_logger
from core.security import generate_csrf_token, hash_password, verify_password
from models.user import User
from services.credential_rotator import rotate_credentials

log = get_logger(__name__)
router = APIRouter(prefix="/security", tags=["Security"])


@router.get("")
async def security_page():
    return RedirectResponse(url="/security/logs", status_code=302)


@router.post("/change-password")
async def change_password():
    raise HTTPException(status_code=400, detail="Password management is disabled. Google OAuth authentication is active.")


@router.post("/rotate-now")
async def manual_rotate_credentials():
    raise HTTPException(status_code=400, detail="Credential rotation is disabled. Google OAuth authentication is active.")


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    lines: int = 200,
    user: User = Depends(require_admin),
):
    """Tail the log file and display in the browser."""
    from core.config import settings
    import os

    log_content = ""
    log_path = settings.log_file
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            log_content = "".join(all_lines[-lines:])

    csrf = generate_csrf_token()
    resp = templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "user": user,
            "log_content": log_content,
            "csrf_token": csrf,
        },
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp
