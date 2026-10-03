"""
tests/test_scheduler.py
------------------------
Unit tests for scheduling logic (no DB or network calls).
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from scheduler.jobs import compute_next_upload_time


class TestComputeNextUploadTime:
    def test_returns_utc_datetime(self):
        result = compute_next_upload_time(14, 20, "US/Eastern")
        assert result.tzinfo is not None
        assert result.tzinfo == timezone.utc

    def test_within_window(self):
        import pytz
        for _ in range(50):
            result = compute_next_upload_time(14, 20, "US/Eastern")
            tz = pytz.timezone("US/Eastern")
            local = result.astimezone(tz)
            assert 14 <= local.hour < 20, f"Hour {local.hour} not in [14, 20)"

    def test_different_timezones(self):
        for tz in ["US/Eastern", "Europe/London", "Asia/Tokyo", "America/Los_Angeles"]:
            result = compute_next_upload_time(8, 12, tz)
            assert result.tzinfo == timezone.utc

    def test_next_day(self):
        """Result should always be in the future (tomorrow)."""
        from datetime import timedelta
        result = compute_next_upload_time(0, 23, "UTC")
        now = datetime.now(timezone.utc)
        assert result > now

    def test_window_randomness(self):
        """Calling multiple times should not always return the same minute."""
        results = {compute_next_upload_time(14, 20, "US/Eastern").minute for _ in range(20)}
        # With 60 possible minutes, we should get at least 2 unique values in 20 tries
        assert len(results) > 1
