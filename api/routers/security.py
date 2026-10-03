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


@router.get("", response_class=HTMLResponse)
async def security_page(
    request: Request,
    user: User = Depends(require_admin),
    msg: str = "",
):
    csrf = generate_csrf_token()
    resp = templates.TemplateResponse(
        "security.html",
        {"request": request, "user": user, "csrf_token": csrf, "msg": msg},
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp


@router.post("/change-password")
async def change_password(
    current_password: str = Form(...),
    new_password: str = Form(..., min_length=12),
    confirm_password: str = Form(...),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if new_password != confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    user.hashed_password = hash_password(new_password)
    await db.flush()
    log.info("Password changed", user_id=user.id)
    return RedirectResponse(url="/security?msg=password_changed", status_code=302)


@router.post("/rotate-now")
async def manual_rotate_credentials(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger credential rotation immediately."""
    await rotate_credentials(db)
    return RedirectResponse(url="/security?msg=rotated", status_code=302)


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
