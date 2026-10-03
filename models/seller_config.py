"""
models/seller_config.py
-----------------------
SQLAlchemy model for storing LicenseAuth / KeyAuth seller keys and user creation settings.
"""

from __future__ import annotations

from typing import Optional
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class SellerConfig(Base, TimestampMixin):
    __tablename__ = "seller_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seller_key: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False, default="Default App")
    api_url: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="https://licenseauth.help/api/seller/",
    )
    sub_name: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    expiry_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_create_on_upload: Mapped[bool] = mapped_column(Boolean, default=True)

    # Status tracking
    last_status: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_tested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
