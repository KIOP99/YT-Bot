"""
services/licenseauth.py
-----------------------
Integration with KeyAuth / LicenseAuth Seller API (e.g. https://licenseauth.help/api/seller/).
Handles creating user/pass accounts, fetching subscriptions, testing seller keys,
and managing seller configs.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import AsyncSessionLocal
from core.logging_config import get_logger
from models.seller_config import SellerConfig

log = get_logger(__name__)

DEFAULT_SELLER_KEY = "5910e2c62eddc63f027d530ba8578fb9"
DEFAULT_API_URL = "https://licenseauth.help/api/seller/"
DEFAULT_SUB = "default"
DEFAULT_EXPIRY_DAYS = 1


async def get_active_seller_config(db: Optional[AsyncSession] = None) -> Optional[SellerConfig]:
    """Retrieve the currently active SellerConfig from the database."""
    async def _query(session: AsyncSession):
        res = await session.execute(
            select(SellerConfig).where(SellerConfig.is_active == True).order_by(SellerConfig.id.desc())
        )
        return res.scalar_one_or_none()

    if db is not None:
        return await _query(db)
    else:
        async with AsyncSessionLocal() as session:
            return await _query(session)


async def call_seller_api(
    seller_key: str,
    action_type: str,
    params: Optional[Dict[str, Any]] = None,
    api_url: Optional[str] = None,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """
    Execute a request against the KeyAuth / LicenseAuth Seller API.
    """
    base_url = (api_url or DEFAULT_API_URL).strip()
    if not base_url.endswith("/"):
        base_url += "/"

    query_params: Dict[str, Any] = {
        "sellerkey": seller_key.strip(),
        "type": action_type.strip(),
        "format": "json",
    }
    if params:
        query_params.update(params)

    try:
        async with httpx.AsyncClient(verify=False, timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(base_url, params=query_params)
            resp.raise_for_status()
            data = resp.json()
            return data
    except httpx.HTTPStatusError as e:
        log.error("LicenseAuth API HTTP error", status_code=e.response.status_code, body=e.response.text)
        return {"success": False, "message": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        log.error("LicenseAuth API connection error", error=str(e))
        return {"success": False, "message": f"Connection error: {str(e)}"}


async def fetch_subscriptions(
    seller_key: str,
    api_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch available subscription plans for the given seller key."""
    return await call_seller_api(seller_key, "fetchallsubs", api_url=api_url)


async def test_seller_key(
    seller_key: str,
    api_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Test a seller key by attempting to fetch its subscriptions."""
    res = await fetch_subscriptions(seller_key, api_url=api_url)
    if res.get("success"):
        subs = res.get("subs", [])
        return {
            "success": True,
            "message": f"Valid Seller Key! Found {len(subs)} subscription(s).",
            "subscriptions": subs,
        }
    return {
        "success": False,
        "message": res.get("message", "Invalid Seller Key or unreachable server."),
        "subscriptions": [],
    }


async def create_licenseauth_user(
    username: str,
    password: str,
    seller_key: Optional[str] = None,
    sub_name: Optional[str] = None,
    expiry_days: Optional[int] = None,
    api_url: Optional[str] = None,
    db: Optional[AsyncSession] = None,
) -> Dict[str, Any]:
    """
    Create a user account (username + password) on LicenseAuth / KeyAuth.
    Uses active SellerConfig or passed parameters.
    """
    key = seller_key
    sub = sub_name
    expiry = expiry_days
    url = api_url

    if not key or not sub or expiry is None:
        cfg = await get_active_seller_config(db)
        if cfg:
            key = key or cfg.seller_key
            sub = sub or cfg.sub_name
            expiry = expiry if expiry is not None else cfg.expiry_days
            url = url or cfg.api_url

    # Fallbacks
    key = key or DEFAULT_SELLER_KEY
    sub = sub or DEFAULT_SUB
    expiry = expiry if expiry is not None else DEFAULT_EXPIRY_DAYS
    url = url or DEFAULT_API_URL

    log.info(
        "Creating LicenseAuth user account",
        user=username,
        sub=sub,
        expiry_days=expiry,
        api_url=url,
    )

    params = {
        "user": username.strip(),
        "pass": password.strip(),
        "sub": sub.strip(),
        "expiry": str(expiry),
    }

    res = await call_seller_api(key, "adduser", params=params, api_url=url)

    if res.get("success"):
        log.info("LicenseAuth user created successfully", user=username)
    else:
        log.warning("LicenseAuth user creation failed", user=username, response=res)

    return res


async def delete_licenseauth_user(
    username: str,
    seller_key: Optional[str] = None,
    api_url: Optional[str] = None,
    db: Optional[AsyncSession] = None,
) -> Dict[str, Any]:
    """Delete a user account from LicenseAuth / KeyAuth."""
    key = seller_key
    url = api_url
    if not key:
        cfg = await get_active_seller_config(db)
        if cfg:
            key = cfg.seller_key
            url = url or cfg.api_url
    key = key or DEFAULT_SELLER_KEY
    url = url or DEFAULT_API_URL

    return await call_seller_api(key, "deluser", params={"user": username.strip()}, api_url=url)


async def auto_create_user_for_video(
    username: str,
    password: str,
    db: Optional[AsyncSession] = None,
) -> Dict[str, Any]:
    """
    Helper to be called during video processing/upload.
    Checks if auto_create_on_upload is enabled, and creates user if so.
    """
    cfg = await get_active_seller_config(db)
    if cfg and not cfg.auto_create_on_upload:
        log.info("LicenseAuth auto-create is disabled in settings; skipping user registration.")
        return {"success": False, "skipped": True, "message": "Auto-creation disabled in settings"}

    return await create_licenseauth_user(
        username=username,
        password=password,
        seller_key=cfg.seller_key if cfg else None,
        sub_name=cfg.sub_name if cfg else None,
        expiry_days=cfg.expiry_days if cfg else None,
        api_url=cfg.api_url if cfg else None,
        db=db,
    )
