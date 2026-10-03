"""
services/ffmpeg.py
-------------------
FFmpeg-based video processing:
  - Probe duration/metadata (with ffprobe or ffmpeg fallback)
  - Trim (start/end)
  - Concatenate intro/outro
  - Burn-in dynamic text overlays (Username & Password at specific timestamps)
  - Re-encode with sensible defaults (H.264 / AAC)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from core.config import settings
from core.logging_config import get_logger

log = get_logger(__name__)


class FFmpegError(Exception):
    """Raised when FFmpeg returns a non-zero exit code."""


def get_ffmpeg_binary() -> str:
    """Return path to ffmpeg executable, checking PATH and imageio_ffmpeg."""
    if shutil.which(settings.ffmpeg_path):
        return settings.ffmpeg_path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return settings.ffmpeg_path


def get_ffprobe_binary() -> Optional[str]:
    """Return path to ffprobe executable if installed."""
    if shutil.which(settings.ffprobe_path):
        return settings.ffprobe_path
    return None


async def probe_video(path: str) -> dict[str, Any]:
    """
    Probe a video file with ffprobe or ffmpeg fallback.
    Returns: duration, width, height, codec, audio_codec, size_bytes.
    """
    ffprobe = get_ffprobe_binary()
    if ffprobe:
        try:
            cmd = [
                ffprobe,
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                data = json.loads(stdout)
                fmt = data.get("format", {})
                video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
                audio_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
                return {
                    "duration": float(fmt.get("duration", 0)),
                    "size_bytes": int(fmt.get("size", os.path.getsize(path) if os.path.exists(path) else 0)),
                    "width": int(video_stream.get("width", 1920)),
                    "height": int(video_stream.get("height", 1080)),
                    "video_codec": video_stream.get("codec_name", "h264"),
                    "audio_codec": audio_stream.get("codec_name", "aac"),
                    "bit_rate": int(fmt.get("bit_rate", 0)),
                }
        except Exception as e:
            log.warning("ffprobe probe failed, trying ffmpeg fallback", error=str(e))

    # Fallback using ffmpeg -i
    ffmpeg = get_ffmpeg_binary()
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-i", path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    output = stderr.decode("utf-8", errors="ignore")

    duration = 0.0
    width = 1920
    height = 1080

    m_dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", output)
    if m_dur:
        hours, mins, secs = m_dur.groups()
        duration = int(hours) * 3600 + int(mins) * 60 + float(secs)

    m_res = re.search(r",\s*(\d{3,4})x(\d{3,4})", output)
    if m_res:
        width = int(m_res.group(1))
        height = int(m_res.group(2))

    size_bytes = os.path.getsize(path) if os.path.exists(path) else 0
    return {
        "duration": duration,
        "size_bytes": size_bytes,
        "width": width,
        "height": height,
        "video_codec": "h264",
        "audio_codec": "aac",
        "bit_rate": 0,
    }


def _build_drawtext_filter(overlay: dict[str, Any], video_height: Optional[int] = None) -> str:
    """Build an FFmpeg drawtext filter string for a given overlay config."""
    text = overlay.get("text", "").replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    start = float(overlay.get("start", 0))
    end = float(overlay.get("end", 10))
    base_fontsize = int(overlay.get("fontsize", 36))

    # Scale font size and box border relative to 1080p baseline so 4K/720p look identical
    if video_height and video_height > 0:
        fontsize = max(16, int(round(base_fontsize * (video_height / 1080.0))))
        boxborderw = max(4, int(round(10 * (video_height / 1080.0))))
    else:
        fontsize = base_fontsize
        boxborderw = 10

    fontcolor = overlay.get("color", "white")
    if fontcolor.startswith("#"):
        fontcolor = f"0x{fontcolor[1:]}"

    position = overlay.get("position", "safe-top-left")
    
    # Check for custom coordinates: custom:x_percent:y_percent (e.g. custom:25.5:70.0)
    if position.startswith("custom:"):
        try:
            parts = position.split(":")
            x_pct = float(parts[1]) / 100.0
            y_pct = float(parts[2]) / 100.0
            x_pct = max(0.0, min(1.0, x_pct))
            y_pct = max(0.0, min(1.0, y_pct))
            coords = f"x=(w-tw)*{x_pct:.4f}:y=(h-th)*{y_pct:.4f}"
        except Exception:
            coords = "x=(w-tw)*0.05:y=(h-th)*0.82"
    else:
        # Resolution-independent safe positions (clear of YouTube scrubber, title, cards)
        # 5% margin from left/right, 7% from top, 18% from bottom
        pos_map = {
            "safe-bottom-left": "x=(w-tw)*0.05:y=(h-th)*0.82",
            "safe-bottom-right": "x=(w-tw)*0.95:y=(h-th)*0.82",
            "safe-bottom-center": "x=(w-tw)/2:y=(h-th)*0.82",
            "safe-top-left": "x=(w-tw)*0.05:y=(h-th)*0.07",
            "safe-top-right": "x=(w-tw)*0.95:y=(h-th)*0.07",
            "safe-top-center": "x=(w-tw)/2:y=(h-th)*0.07",
            "center": "x=(w-tw)/2:y=(h-th)/2",

            # Legacy edge presets
            "top-left": "x=(w-tw)*0.05:y=(h-th)*0.07",
            "top-right": "x=(w-tw)*0.95:y=(h-th)*0.07",
            "bottom-left": "x=(w-tw)*0.05:y=(h-th)*0.82",
            "bottom-right": "x=(w-tw)*0.95:y=(h-th)*0.82",
            "top-center": "x=(w-tw)/2:y=(h-th)*0.07",
            "bottom-center": "x=(w-tw)/2:y=(h-th)*0.82",
        }
        coords = pos_map.get(position, "x=(w-tw)*0.05:y=(h-th)*0.82")

    always_visible = overlay.get("always_visible", False)
    enable_clause = overlay.get("enable_clause")
    if enable_clause:
        enable_str = f":enable='{enable_clause}'"
    elif always_visible:
        enable_str = ""
    else:
        enable_str = f":enable='between(t,{start},{end})'"

    return (
        f"drawtext=text='{text}':{coords}:fontcolor={fontcolor}:fontsize={fontsize}:"
        f"box=1:boxcolor=black@0.75:boxborderw={boxborderw}"
        f"{enable_str}"
    )


async def render_real_frame_preview(
    video_path: str,
    timestamp: float,
    overlays: list[dict[str, Any]],
    output_path: str,
) -> dict[str, Any]:
    """
    Extract a single frame at `timestamp` with drawtext overlays burned in,
    providing a 100% accurate WYSIWYG preview of how FFmpeg renders the overlays.
    """
    workdir = Path(output_path).parent
    workdir.mkdir(parents=True, exist_ok=True)

    info = await probe_video(video_path)
    duration = info.get("duration", 0.0)
    video_height = info.get("height", 1080)
    video_width = info.get("width", 1920)

    # Clamp timestamp
    t = max(0.0, min(duration - 0.1 if duration > 0.5 else 0.0, float(timestamp)))

    vf_filters = []
    for ov in overlays:
        if ov.get("enabled", True) and ov.get("text"):
            ov_copy = dict(ov)
            ov_copy["always_visible"] = True
            vf_filters.append(_build_drawtext_filter(ov_copy, video_height=video_height))

    ffmpeg = get_ffmpeg_binary()
    cmd = [
        ffmpeg, "-y",
        "-ss", f"{t:.3f}",
        "-i", video_path,
    ]
    if vf_filters:
        cmd += ["-vf", ",".join(vf_filters)]
    cmd += [
        "-vframes", "1",
        "-q:v", "2",
        output_path,
    ]

    await _run_ffmpeg(cmd)

    return {
        "output_path": output_path,
        "width": video_width,
        "height": video_height,
        "duration": duration,
        "timestamp": t,
        "overlays_rendered": len(vf_filters),
    }


async def process_video(
    input_path: str,
    output_path: str,
    trim_start: Optional[float] = None,
    trim_end: Optional[float] = None,
    intro_path: Optional[str] = None,
    outro_path: Optional[str] = None,
    overlays: Optional[list[dict[str, Any]]] = None,
    thumbnail_path: Optional[str] = None,
    thumbnail_intro_duration: float = 1.0,
) -> str:
    """
    Apply FFmpeg edits and write to output_path.
    Processing order: trim → concat intro/outro → re-encode with thumbnail intro & text overlays.
    Returns the final output_path.
    """
    workdir = Path(output_path).parent
    workdir.mkdir(parents=True, exist_ok=True)

    current_path = input_path

    # ── Step 1: Trim ────────────────────────────────────────────────────────
    if trim_start is not None or trim_end is not None:
        trimmed = str(workdir / "_trimmed.mp4")
        await _trim(current_path, trimmed, trim_start, trim_end)
        current_path = trimmed

    # ── Step 2: Concat intro / outro ────────────────────────────────────────
    if intro_path or outro_path:
        segments = []
        if intro_path and os.path.exists(intro_path):
            segments.append(intro_path)
        segments.append(current_path)
        if outro_path and os.path.exists(outro_path):
            segments.append(outro_path)
        if len(segments) > 1:
            concat_out = str(workdir / "_concat.mp4")
            await _concat_segments(segments, concat_out)
            current_path = concat_out

    # ── Step 3: Re-encode with thumbnail embed & drawtext overlays ──────────
    vf_filters = []
    if overlays:
        try:
            info = await probe_video(current_path)
            vid_h = info.get("height", 1080)
        except Exception:
            vid_h = 1080

        for ov in overlays:
            if ov.get("enabled", True) and ov.get("text"):
                vf_filters.append(_build_drawtext_filter(ov, video_height=vid_h))

    await _reencode(
        current_path,
        output_path,
        vf_filters=vf_filters if vf_filters else None,
        thumbnail_path=thumbnail_path,
        thumbnail_intro_duration=thumbnail_intro_duration,
    )

    log.info(
        "Video processing complete",
        output=output_path,
        overlay_count=len(vf_filters),
        thumbnail_embedded=bool(thumbnail_path and os.path.exists(thumbnail_path)),
    )
    return output_path


async def _trim(
    input_path: str,
    output_path: str,
    start: Optional[float],
    end: Optional[float],
) -> None:
    ffmpeg = get_ffmpeg_binary()
    cmd = [ffmpeg, "-y"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", input_path]
    if end:
        cmd += ["-to", str(end)]
    cmd += ["-c", "copy", output_path]
    await _run_ffmpeg(cmd)


async def _concat_segments(segments: list[str], output_path: str) -> None:
    """Concatenate video segments using the concat demuxer."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        for seg in segments:
            f.write(f"file '{seg}'\n")
        list_file = f.name
    try:
        ffmpeg = get_ffmpeg_binary()
        cmd = [
            ffmpeg, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            output_path,
        ]
        await _run_ffmpeg(cmd)
    finally:
        try:
            os.unlink(list_file)
        except Exception:
            pass


async def _reencode(
    input_path: str,
    output_path: str,
    vf_filters: Optional[list[str]] = None,
    thumbnail_path: Optional[str] = None,
    thumbnail_intro_duration: float = 1.0,
) -> None:
    """Re-encode to H.264 + AAC with optional video filters and optional thumbnail embedding."""
    ffmpeg = get_ffmpeg_binary()

    if thumbnail_path and os.path.exists(thumbnail_path):
        # Embed thumbnail into opening frame / first second of the video
        info = await probe_video(input_path)
        vid_w = info.get("width", 1280)
        vid_h = info.get("height", 720)
        dur = info.get("duration", 0.0)
        thumb_dur = min(thumbnail_intro_duration, max(0.2, dur * 0.5)) if dur > 0 else thumbnail_intro_duration

        # Scale and pad thumbnail to match exact video resolution, then overlay from t=0 to t=thumb_dur
        scale_pad = f"scale={vid_w}:{vid_h}:force_original_aspect_ratio=decrease,pad={vid_w}:{vid_h}:(ow-iw)/2:(oh-ih)/2"

        if vf_filters:
            filter_complex = (
                f"[1:v]{scale_pad}[thumb_scaled];"
                f"[0:v][thumb_scaled]overlay=0:0:enable='between(t,0,{thumb_dur:.3f})'[v_thumb];"
                f"[v_thumb]{','.join(vf_filters)}[outv]"
            )
        else:
            filter_complex = (
                f"[1:v]{scale_pad}[thumb_scaled];"
                f"[0:v][thumb_scaled]overlay=0:0:enable='between(t,0,{thumb_dur:.3f})'[outv]"
            )

        cmd = [
            ffmpeg, "-y",
            "-i", input_path,
            "-i", thumbnail_path,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "22",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        cmd = [
            ffmpeg, "-y",
            "-i", input_path,
        ]
        if vf_filters:
            cmd += ["-vf", ",".join(vf_filters)]

        cmd += [
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "22",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            output_path,
        ]

    await _run_ffmpeg(cmd)


async def _run_ffmpeg(cmd: list[str]) -> None:
    """Run an FFmpeg command asynchronously."""
    log.debug("Running FFmpeg", cmd=" ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise FFmpegError(f"FFmpeg exited {proc.returncode}: {stderr.decode(errors='ignore')[-2000:]}")
