"""
tests/test_thumbnail_rotation.py
----------------------------------
Tests for thumbnail validation and pool selection logic.
"""

import os
import tempfile
import pytest
from PIL import Image

from services.thumbnail import ThumbnailValidationError, validate_thumbnail


def make_test_image(width: int, height: int, fmt: str = "JPEG") -> tuple[str, int]:
    """Create a temporary test image. Returns (path, size)."""
    suffix = ".jpg" if fmt == "JPEG" else ".png"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        img = Image.new("RGB", (width, height), color=(128, 0, 0))
        img.save(f.name, fmt)
        size = os.path.getsize(f.name)
        return f.name, size


class TestValidateThumbnail:
    def test_valid_jpeg_hd(self):
        path, size = make_test_image(1280, 720, "JPEG")
        try:
            w, h = validate_thumbnail(path, size)
            assert w == 1280
            assert h == 720
        finally:
            os.unlink(path)

    def test_valid_4k(self):
        path, size = make_test_image(3840, 2160, "PNG")
        try:
            w, h = validate_thumbnail(path, size)
            assert w == 3840
        finally:
            os.unlink(path)

    def test_too_small(self):
        path, size = make_test_image(640, 360, "JPEG")
        try:
            with pytest.raises(ThumbnailValidationError, match="too small"):
                validate_thumbnail(path, size)
        finally:
            os.unlink(path)

    def test_wrong_aspect_ratio(self):
        path, size = make_test_image(1280, 960, "JPEG")  # 4:3
        try:
            with pytest.raises(ThumbnailValidationError, match="Aspect ratio"):
                validate_thumbnail(path, size)
        finally:
            os.unlink(path)

    def test_file_too_large(self):
        path, _ = make_test_image(1280, 720, "JPEG")
        try:
            huge_size = 11 * 1024 * 1024  # 11 MB (over 10 MB limit)
            with pytest.raises(ThumbnailValidationError, match="too large"):
                validate_thumbnail(path, huge_size)
        finally:
            os.unlink(path)


class TestExtractFrameAtTimestamp:
    @pytest.mark.asyncio
    async def test_extract_1s_frame(self):
        import asyncio
        from pathlib import Path
        from services.ffmpeg import get_ffmpeg_binary
        from services.thumbnail import extract_frame_at_timestamp

        ffmpeg = get_ffmpeg_binary()
        tmp_video = "tests/test_sample_1s.mp4"
        tmp_thumb = "tests/test_sample_1s_thumb.jpg"

        # Generate a small 3-second synthetic test video
        cmd_gen = [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=1280x720:rate=25",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            tmp_video
        ]
        proc = await asyncio.create_subprocess_exec(*cmd_gen, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.communicate()
        assert os.path.exists(tmp_video)

        try:
            w, h, size = await extract_frame_at_timestamp(tmp_video, tmp_thumb, target_sec=1.0)
            assert w == 1280
            assert h == 720
            assert size > 0
            assert size <= 2 * 1024 * 1024
            assert os.path.exists(tmp_thumb)
        finally:
            if os.path.exists(tmp_video):
                os.unlink(tmp_video)
            if os.path.exists(tmp_thumb):
                os.unlink(tmp_thumb)


class TestYouTubeSetThumbnail:
    def test_set_thumbnail_file_not_found(self):
        from services.youtube import YouTubeService
        svc = YouTubeService.__new__(YouTubeService)
        with pytest.raises(FileNotFoundError):
            svc.set_thumbnail("dummy_video_id", "non_existent_file_path_123.jpg")


class TestStandardizeThumbnail:
    def test_standardize_1080p_to_720p(self):
        """Test that a large 1920x1080 thumbnail is automatically standardized to working size 1280x720."""
        from services.thumbnail import standardize_thumbnail
        path, _ = make_test_image(1920, 1080, "JPEG")
        try:
            w, h, size = standardize_thumbnail(path)
            assert w == 1280
            assert h == 720
            assert size > 0
            assert size <= 2 * 1024 * 1024

            with Image.open(path) as img:
                assert img.size == (1280, 720)
                assert img.format == "JPEG"
        finally:
            os.unlink(path)

    def test_standardize_4k_png_to_720p_jpeg(self):
        """Test that a 4K PNG is converted to 1280x720 JPEG under 2MB."""
        from services.thumbnail import standardize_thumbnail
        path, _ = make_test_image(3840, 2160, "PNG")
        try:
            w, h, size = standardize_thumbnail(path)
            assert w == 1280
            assert h == 720
            assert size <= 2 * 1024 * 1024

            with Image.open(path) as img:
                assert img.size == (1280, 720)
                assert img.format == "JPEG"
        finally:
            os.unlink(path)

    def test_standardize_non_16_9_letterbox(self):
        """Test that a 4:3 image (e.g. 1024x768) is letterboxed to 1280x720."""
        from services.thumbnail import standardize_thumbnail
        path, _ = make_test_image(1024, 768, "JPEG")
        try:
            w, h, size = standardize_thumbnail(path)
            assert w == 1280
            assert h == 720
            with Image.open(path) as img:
                assert img.size == (1280, 720)
        finally:
            os.unlink(path)


class TestEmbedThumbnailInVideo:
    @pytest.mark.asyncio
    async def test_embed_thumbnail_in_video_intro(self):
        """Test that process_video with thumbnail_path embeds the thumbnail at t=0s of the video."""
        import asyncio
        from services.ffmpeg import get_ffmpeg_binary, process_video
        from PIL import ImageDraw

        ffmpeg = get_ffmpeg_binary()
        tmp_video = "tests/test_raw_video.mp4"
        tmp_thumb = "tests/test_embed_thumb.jpg"
        tmp_out = "tests/test_embed_output.mp4"

        # 1. Create a 3s test video
        cmd_gen = [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=1280x720:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
            "-c:v", "libx264", "-c:a", "aac",
            tmp_video
        ]
        proc = await asyncio.create_subprocess_exec(*cmd_gen, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.communicate()
        assert os.path.exists(tmp_video)

        # 2. Create a solid blue thumbnail image
        img = Image.new("RGB", (1280, 720), color=(0, 0, 255))
        img.save(tmp_thumb, "JPEG")

        try:
            # 3. Process video embedding the thumbnail
            out = await process_video(
                input_path=tmp_video,
                output_path=tmp_out,
                thumbnail_path=tmp_thumb,
                thumbnail_intro_duration=1.0,
            )
            assert os.path.exists(out)

            # 4. Extract frame at 0.2s from output video — must be the blue thumbnail!
            tmp_frame = "tests/test_extracted_frame.jpg"
            cmd_frame = [
                ffmpeg, "-y",
                "-ss", "0.2",
                "-i", out,
                "-vframes", "1",
                tmp_frame
            ]
            p2 = await asyncio.create_subprocess_exec(*cmd_frame, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await p2.communicate()
            assert os.path.exists(tmp_frame)

            with Image.open(tmp_frame) as f_img:
                center_pixel = f_img.getpixel((640, 360))
                # Blue component should be dominant (>200) and red/green low (<50)
                assert center_pixel[2] > 200, f"Expected blue dominant pixel, got {center_pixel}"
                assert center_pixel[0] < 50
                assert center_pixel[1] < 50

            if os.path.exists(tmp_frame):
                os.unlink(tmp_frame)
        finally:
            for p in (tmp_video, tmp_thumb, tmp_out):
                if os.path.exists(p):
                    os.unlink(p)

