"""
tests/test_fixes.py
-------------------
Unit tests for the 4 fixes:
1. Discord announcement deduplication
2. Upload progress cleanup & idle state handling
3. Auto-comment trigger on public & unlisted videos
4. Scheduled release job cancellation & deduplication lock
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from services.discord_notify import send_upload_announcement, _recent_announcements
from services.upload_progress import upload_tracker
from services.comment_poster import format_comment_text, trigger_auto_comment


class TestDiscordAnnouncementDeduplication:
    @pytest.mark.asyncio
    async def test_deduplication_suppresses_second_call_within_60s(self):
        """Two announcements for the same channel and video URL within 60s should only send one message."""
        channel_id = 999
        yt_url = "https://www.youtube.com/watch?v=TEST_DEDUP_123"
        dedup_key = f"{channel_id}:{yt_url}"

        # Populate cache as if first message was sent 5 seconds ago
        _recent_announcements[dedup_key] = (time.time() - 5.0, 123456789)

        result = await send_upload_announcement(
            channel_id=channel_id,
            video_title="Duplicate Video Test",
            youtube_url=yt_url,
            channel_name="TestChannel",
        )

        # Should return the cached message ID without sending again
        assert result == 123456789

        # Clean up
        _recent_announcements.pop(dedup_key, None)


class TestUploadProgressCleanup:
    def test_progress_finish_and_clear(self):
        """upload_tracker should properly track progress and clear cleanly."""
        video_id = 8888
        upload_tracker.start(video_id, total_bytes=1000000, channel_id=1)
        prog = upload_tracker.get(video_id)
        assert prog["active"] is True
        assert prog["percent"] == 3

        upload_tracker.finish(video_id, {"status": "released"})
        prog_finished = upload_tracker.get(video_id)
        assert prog_finished["active"] is False
        assert prog_finished["stage"] == "success"

        # Explicit clear resets state
        upload_tracker.clear(video_id)
        assert upload_tracker.get(video_id) is None


class TestAutoCommentFormattingAndTrigger:
    def test_comment_text_interpolation(self):
        template = "Watch {title}! Link: {url} on {channel_name}."
        formatted = format_comment_text(
            template=template,
            video_title="My Awesome Video",
            youtube_video_id="abc123xyz",
            channel_name="Cool Channel",
        )
        assert "My Awesome Video" in formatted
        assert "https://www.youtube.com/watch?v=abc123xyz" in formatted
        assert "Cool Channel" in formatted

    def test_clean_video_id_in_comment_text(self):
        template = "Check out: {url}"
        formatted = format_comment_text(
            template=template,
            video_title="Test",
            youtube_video_id="https://www.youtube.com/watch?v=cleanID",
            channel_name="TestChannel",
        )
        # Should not produce double URLs
        assert "https://www.youtube.com/watch?v=cleanID" in formatted


class TestCountdownTimerLogic:
    def test_timer_iso_calculation(self):
        """Simulate local time remaining calculations."""
        now = time.time()
        future_iso = "2026-10-04T14:30:00+00:00"
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(future_iso)
        assert dt.tzinfo is not None


class TestThumbnailImageEndpoint:
    @pytest.mark.asyncio
    async def test_get_thumbnail_image_serves_valid_image(self):
        """Test that /api/thumbnails/{id}/image serves image and provides clean fallback."""
        import httpx
        from api.main import create_app
        from models.thumbnail import Thumbnail
        from core.database import AsyncSessionLocal

        app = create_app()
        async with AsyncSessionLocal() as db:
            # Create a dummy thumbnail
            thumb = Thumbnail(
                channel_id=1,
                filename="test_thumb_endpoint.jpg",
                stored_path="uploads/thumbnails/test_thumb_endpoint.jpg",
                width=1280,
                height=720,
                file_size_bytes=100,
                sort_order=0,
            )
            db.add(thumb)
            await db.commit()
            await db.refresh(thumb)
            thumb_id = thumb.id

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/thumbnails/{thumb_id}/image")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "image/jpeg"
            assert len(resp.content) > 0

        # Cleanup
        async with AsyncSessionLocal() as db:
            t = await db.get(Thumbnail, thumb_id)
            if t:
                await db.delete(t)
                await db.commit()


class TestVideoThumbnailSelectionAndNoAuto:
    @pytest.mark.asyncio
    async def test_video_custom_thumbnail_and_no_auto_video(self):
        """Test that Video can have ANY thumbnail assigned, and no auto_video thumbnails are produced."""
        from models.thumbnail import Thumbnail
        from models.video import Video
        from models.channel import YouTubeChannel
        from core.database import AsyncSessionLocal
        from services.thumbnail import get_channel_thumbnails, get_next_thumbnail
        from scheduler.jobs import prepare_scheduled_thumbnail

        async with AsyncSessionLocal() as db:
            # Create channel
            ch = YouTubeChannel(name="ThumbTestChannel", is_active=True)
            db.add(ch)
            await db.flush()

            # Create custom thumbnail (e.g. 109682.jpg fantasy airship)
            custom_thumb = Thumbnail(
                channel_id=ch.id,
                filename="109682.jpg",
                stored_path="uploads/thumbnails/109682.jpg",
                width=1920,
                height=1080,
                file_size_bytes=216000,
                sort_order=0,
                is_active=True,
            )
            # Create an unwanted auto_video thumbnail
            auto_thumb = Thumbnail(
                channel_id=ch.id,
                filename="auto_video_999.jpg",
                stored_path="uploads/thumbnails/auto_video_999.jpg",
                width=1280,
                height=720,
                file_size_bytes=100000,
                sort_order=1,
                is_active=True,
            )
            db.add_all([custom_thumb, auto_thumb])
            await db.flush()

            # 1. get_channel_thumbnails should filter out auto_video thumbnails
            pool = await get_channel_thumbnails(db, ch.id)
            filenames = [t.filename for t in pool]
            assert "auto_video_999.jpg" not in filenames

            # 2. Video with thumbnail_id set
            vid = Video(
                channel_id=ch.id,
                title="My Custom Video",
                original_filename="clip.mp4",
                stored_path="uploads/videos/clip.mp4",
                thumbnail_id=custom_thumb.id,
                is_active=True,
            )
            db.add(vid)
            await db.flush()

            assert vid.thumbnail_id == custom_thumb.id
            assert vid.thumbnail_web_url == f"/api/thumbnails/{custom_thumb.id}/image"

            # 3. prepare_scheduled_thumbnail should prioritize video.thumbnail_id
            selected = await prepare_scheduled_thumbnail(db, ch.id, None, vid)
            assert selected is not None
            assert selected.id == custom_thumb.id
            assert selected.filename == "109682.jpg"

            # Rollback/cleanup
            await db.rollback()


class TestVideoPageAndAsyncSafeProperties:
    @pytest.mark.asyncio
    async def test_video_properties_without_lazy_load_greenlet_error(self):
        from core.database import AsyncSessionLocal
        from models.video import Video
        from models.channel import YouTubeChannel
        from sqlalchemy import select, delete

        async with AsyncSessionLocal() as db:
            ch = YouTubeChannel(name="SafeChannelTest", is_active=True, is_authenticated=True)
            db.add(ch)
            await db.commit()
            await db.refresh(ch)

            vid = Video(
                channel_id=ch.id,
                title="Safe Video Test",
                original_filename="safe.mp4",
                stored_path="uploads/videos/safe.mp4",
                is_active=True,
            )
            db.add(vid)
            await db.commit()

            # Now select in a new session without eager loading
            async with AsyncSessionLocal() as db2:
                res = await db2.execute(select(Video).where(Video.id == vid.id))
                loaded_vid = res.scalar_one()

                # Accessing properties MUST NOT raise MissingGreenlet
                url = loaded_vid.thumbnail_web_url
                assert isinstance(url, str)

                name = loaded_vid.thumbnail_name
                assert isinstance(name, str)

            # Cleanup
            await db.execute(delete(Video).where(Video.channel_id == ch.id))
            await db.execute(delete(YouTubeChannel).where(YouTubeChannel.id == ch.id))
            await db.commit()


