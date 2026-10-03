"""
services/credential_rotator.py
--------------------------------
Automatically generates new dashboard credentials on a schedule,
stores the Argon2 hash in the DB, and notifies the admin via Discord DM.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging_config import get_logger
from core.security import generate_secure_password, generate_secure_username, hash_password
from models.user import User
from services.discord_notify import alert_new_credentials

log = get_logger(__name__)


async def rotate_credentials(db: AsyncSession) -> None:
    """
    Generate new username/password, hash them, update the admin user in the DB,
    and notify via Discord DM.

    Designed to be called from the APScheduler job.
    """
    result = await db.execute(select(User).where(User.id == 1))
    user: User | None = result.scalar_one_or_none()

    if user is None:
        log.error("Admin user (id=1) not found; skipping credential rotation")
        return

    new_username = generate_secure_username()
    new_password = generate_secure_password()

    user.username = new_username
    user.hashed_password = hash_password(new_password)
    user.last_rotation_at = datetime.now(timezone.utc).isoformat()
    user.rotation_count += 1

    await db.flush()
    log.info(
        "Credentials rotated",
        new_username=new_username,
        rotation_count=user.rotation_count,
    )

    # Notify admin — password is plain-text here briefly (never stored)
    await alert_new_credentials(new_username, new_password)
    del new_password  # best-effort memory cleanup
