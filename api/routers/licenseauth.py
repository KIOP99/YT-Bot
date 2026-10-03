"""
api/routers/licenseauth.py
--------------------------
Web routes and API endpoints for managing LicenseAuth / KeyAuth Seller API integrations,
managing multiple seller keys, testing connections, and configuring auto-user generation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import require_admin
from api.templates import templates
from core.config import settings
from core.database import get_db
from core.logging_config import get_logger
from core.security import generate_csrf_token
from models.seller_config import SellerConfig
from services.licenseauth import (
    create_licenseauth_user,
    fetch_subscriptions,
    get_active_seller_config,
    test_seller_key,
)

log = get_logger(__name__)
router = APIRouter(prefix="/license-auth", tags=["LicenseAuth"])


@router.get("", response_class=HTMLResponse)
async def licenseauth_page(
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render the LicenseAuth / KeyAuth management dashboard page."""
    # Ensure default key exists in DB if table is empty
    res = await db.execute(select(SellerConfig))
    all_keys = res.scalars().all()
    if not all_keys:
        default_cfg = SellerConfig(
            seller_key=settings.licenseauth_seller_key,
            label="Main Application",
            api_url=settings.licenseauth_api_url,
            sub_name=settings.licenseauth_sub_name,
            expiry_days=settings.licenseauth_expiry_days,
            level=1,
            is_active=True,
            auto_create_on_upload=settings.licenseauth_auto_create,
            last_status="Initialized",
            last_tested_at=datetime.utcnow(),
        )
        db.add(default_cfg)
        await db.commit()
        await db.refresh(default_cfg)
        all_keys = [default_cfg]

    active_cfg = next((k for k in all_keys if k.is_active), all_keys[0])

    # Fetch live subscriptions for active key
    subs = []
    try:
        sub_res = await fetch_subscriptions(active_cfg.seller_key, api_url=active_cfg.api_url)
        if sub_res.get("success"):
            subs = sub_res.get("subs", [])
    except Exception:
        pass

    csrf = generate_csrf_token()
    resp = templates.TemplateResponse(
        "licenseauth.html",
        {
            "request": request,
            "user": user,
            "keys": all_keys,
            "active_config": active_cfg,
            "subscriptions": subs,
            "csrf_token": csrf,
        },
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp


@router.get("/keys")
async def list_seller_keys(
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all registered seller keys."""
    res = await db.execute(select(SellerConfig).order_by(SellerConfig.id.asc()))
    keys = res.scalars().all()
    return {
        "success": True,
        "keys": [
            {
                "id": k.id,
                "label": k.label,
                "seller_key_masked": k.seller_key[:8] + "..." + k.seller_key[-4:] if len(k.seller_key) > 12 else k.seller_key,
                "api_url": k.api_url,
                "sub_name": k.sub_name,
                "expiry_days": k.expiry_days,
                "level": k.level,
                "is_active": k.is_active,
                "auto_create_on_upload": k.auto_create_on_upload,
                "last_status": k.last_status,
                "last_tested_at": k.last_tested_at.isoformat() if k.last_tested_at else None,
            }
            for k in keys
        ],
    }


@router.post("/keys")
async def add_seller_key(
    seller_key: str = Form(...),
    label: str = Form("Application Key"),
    api_url: str = Form("https://licenseauth.help/api/seller/"),
    sub_name: str = Form("default"),
    expiry_days: int = Form(1),
    level: int = Form(1),
    set_as_active: bool = Form(True),
    auto_create_on_upload: bool = Form(True),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Register a new seller key in the website."""
    seller_key = seller_key.strip()
    if not seller_key:
        raise HTTPException(status_code=400, detail="Seller key cannot be empty")

    # If setting as active, deactivate other keys
    if set_as_active:
        await db.execute(update(SellerConfig).values(is_active=False))

    new_cfg = SellerConfig(
        seller_key=seller_key,
        label=label.strip() or "Application Key",
        api_url=api_url.strip() or "https://licenseauth.help/api/seller/",
        sub_name=sub_name.strip() or "default",
        expiry_days=max(1, expiry_days),
        level=max(1, level),
        is_active=set_as_active,
        auto_create_on_upload=auto_create_on_upload,
        last_status="Added",
        last_tested_at=None,
    )
    db.add(new_cfg)
    await db.commit()
    await db.refresh(new_cfg)

    # Automatically test key
    test_res = await test_seller_key(new_cfg.seller_key, api_url=new_cfg.api_url)
    new_cfg.last_status = test_res.get("message", "Tested")
    new_cfg.last_tested_at = datetime.utcnow()
    await db.commit()

    return {
        "success": True,
        "message": f"Seller key '{new_cfg.label}' added successfully!",
        "key_id": new_cfg.id,
        "test_result": test_res,
    }


@router.post("/keys/{key_id}/activate")
async def activate_seller_key(
    key_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set the specified seller key as active."""
    target = await db.get(SellerConfig, key_id)
    if not target:
        raise HTTPException(status_code=404, detail="Seller key not found")

    await db.execute(update(SellerConfig).values(is_active=False))
    target.is_active = True
    await db.commit()

    return {
        "success": True,
        "message": f"Seller key '{target.label}' is now ACTIVE for automated uploads!",
        "key_id": target.id,
    }


@router.post("/keys/{key_id}/test")
async def test_key_endpoint(
    key_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Test a specific seller key against the LicenseAuth API."""
    target = await db.get(SellerConfig, key_id)
    if not target:
        raise HTTPException(status_code=404, detail="Seller key not found")

    test_res = await test_seller_key(target.seller_key, api_url=target.api_url)
    target.last_status = test_res.get("message", "Tested")
    target.last_tested_at = datetime.utcnow()
    await db.commit()

    return test_res


@router.delete("/keys/{key_id}")
async def delete_seller_key(
    key_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove a seller key."""
    target = await db.get(SellerConfig, key_id)
    if not target:
        raise HTTPException(status_code=404, detail="Seller key not found")

    was_active = target.is_active
    await db.delete(target)
    await db.commit()

    # If the deleted key was active, activate the first available key
    if was_active:
        res = await db.execute(select(SellerConfig).order_by(SellerConfig.id.asc()))
        remaining = res.scalars().all()
        if remaining:
            remaining[0].is_active = True
            await db.commit()

    return {"success": True, "message": f"Seller key '{target.label}' deleted."}


@router.post("/config")
async def update_active_config(
    sub_name: str = Form(...),
    expiry_days: int = Form(1),
    auto_create_on_upload: bool = Form(True),
    api_url: Optional[str] = Form(None),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update settings for the currently active SellerConfig."""
    cfg = await get_active_seller_config(db)
    if not cfg:
        raise HTTPException(status_code=404, detail="No active seller key found. Please add one first.")

    cfg.sub_name = sub_name.strip()
    cfg.expiry_days = max(1, expiry_days)
    cfg.auto_create_on_upload = auto_create_on_upload
    if api_url:
        cfg.api_url = api_url.strip()

    await db.commit()
    await db.refresh(cfg)

    return {
        "success": True,
        "message": "LicenseAuth settings updated successfully!",
        "config": {
            "sub_name": cfg.sub_name,
            "expiry_days": cfg.expiry_days,
            "auto_create_on_upload": cfg.auto_create_on_upload,
            "api_url": cfg.api_url,
        },
    }


@router.post("/create-user")
async def manual_create_user(
    username: str = Form(...),
    password: str = Form(...),
    sub_name: Optional[str] = Form(None),
    expiry_days: Optional[int] = Form(None),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manually create a user/pass account on LicenseAuth."""
    username = username.strip()
    password = password.strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and Password cannot be empty")

    res = await create_licenseauth_user(
        username=username,
        password=password,
        sub_name=sub_name,
        expiry_days=expiry_days,
        db=db,
    )
    return res


@router.get("/subscriptions")
async def get_subs_endpoint(
    seller_key: Optional[str] = None,
    api_url: Optional[str] = None,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Fetch live subscriptions for a given key or active key."""
    key = seller_key
    url = api_url
    if not key:
        cfg = await get_active_seller_config(db)
        if cfg:
            key = cfg.seller_key
            url = url or cfg.api_url
    if not key:
        key = settings.licenseauth_seller_key

    return await fetch_subscriptions(key, api_url=url)
