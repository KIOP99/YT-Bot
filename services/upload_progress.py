"""
services/upload_progress.py
---------------------------
In-memory real-time progress tracker for video processing and YouTube uploads.
Enables the frontend to poll and render dynamic progress bars, MB counters,
upload speed, ETA, and stages during test or scheduled uploads.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional


class UploadProgressTracker:
    def __init__(self) -> None:
        self._progress: Dict[int, Dict[str, Any]] = {}
        self._history: Dict[int, list[tuple[float, int]]] = {}
        self._channel_map: Dict[int, int] = {}

    def start(self, video_id: int, total_bytes: int = 0, channel_id: Optional[int] = None) -> None:
        """Initialize progress tracking for a video upload."""
        total_mb = round(total_bytes / (1024 * 1024), 1) if total_bytes > 0 else 0.0
        if channel_id:
            self._channel_map[channel_id] = video_id
        self._progress[video_id] = {
            "active": True,
            "stage": "preparing",
            "stage_label": "Preparing video & configuration...",
            "percent": 3,
            "percent_left": 97,
            "bytes_uploaded": 0,
            "total_bytes": total_bytes,
            "uploaded_mb": 0.0,
            "total_mb": total_mb,
            "remaining_mb": total_mb,
            "speed_mb": 0.0,
            "eta_seconds": 0,
            "channel_id": channel_id,
            "video_id": video_id,
            "error": None,
            "result": None,
            "started_at": time.monotonic(),
            "updated_at": time.monotonic(),
        }
        self._history[video_id] = [(time.monotonic(), 0)]

    def update_stage(self, video_id: int, stage: str, stage_label: str, percent: int) -> None:
        """Update the high-level processing stage."""
        if video_id not in self._progress:
            self.start(video_id)
        prog = self._progress[video_id]
        prog["stage"] = stage
        prog["stage_label"] = stage_label
        prog["percent"] = max(prog.get("percent", 0), percent)
        prog["percent_left"] = max(0, 100 - prog["percent"])
        prog["updated_at"] = time.monotonic()

    def update_bytes(self, video_id: int, bytes_uploaded: int, total_bytes: int) -> None:
        """Update live upload bytes and calculate speed / ETA."""
        if video_id not in self._progress:
            self.start(video_id, total_bytes)

        now = time.monotonic()
        prog = self._progress[video_id]

        if total_bytes > 0:
            prog["total_bytes"] = total_bytes
            prog["total_mb"] = round(total_bytes / (1024 * 1024), 1)

        prog["bytes_uploaded"] = bytes_uploaded
        prog["uploaded_mb"] = round(bytes_uploaded / (1024 * 1024), 1)

        # Scale progress from 15% (after ffmpeg/thumb) to 95% (during YouTube upload)
        if total_bytes > 0:
            raw_pct = min(1.0, max(0.0, bytes_uploaded / total_bytes))
            prog["percent"] = int(15 + (raw_pct * 80))
            prog["percent_left"] = max(0, 100 - prog["percent"])
            prog["remaining_mb"] = max(0.0, round(prog["total_mb"] - prog["uploaded_mb"], 1))
            prog["stage"] = "uploading"
            prog["stage_label"] = (
                f"Uploading to YouTube: {prog['uploaded_mb']} MB / {prog['total_mb']} MB ({int(raw_pct * 100)}%)"
            )

        # Calculate speed with sliding window
        hist = self._history.setdefault(video_id, [])
        hist.append((now, bytes_uploaded))
        # Keep last 5 samples
        if len(hist) > 8:
            hist.pop(0)

        speed_mb = 0.0
        eta_seconds = 0
        if len(hist) >= 2:
            dt = hist[-1][0] - hist[0][0]
            db = hist[-1][1] - hist[0][1]
            if dt > 0.3 and db > 0:
                bytes_per_sec = db / dt
                speed_mb = round(bytes_per_sec / (1024 * 1024), 2)
                remaining_bytes = max(0, (total_bytes or 0) - bytes_uploaded)
                if bytes_per_sec > 0:
                    eta_seconds = int(remaining_bytes / bytes_per_sec)

        prog["speed_mb"] = speed_mb
        prog["eta_seconds"] = eta_seconds
        prog["updated_at"] = now

    def finish(self, video_id: int, result: Dict[str, Any]) -> None:
        """Mark upload as successfully finished."""
        if video_id in self._progress:
            prog = self._progress[video_id]
            prog["active"] = False
            prog["stage"] = "success"
            prog["stage_label"] = "🎉 Upload Complete!"
            prog["percent"] = 100
            prog["percent_left"] = 0
            prog["remaining_mb"] = 0.0
            prog["result"] = result
            prog["updated_at"] = time.monotonic()

    def fail(self, video_id: int, error_message: str) -> None:
        """Mark upload as failed with error details."""
        if video_id in self._progress:
            prog = self._progress[video_id]
            prog["active"] = False
            prog["stage"] = "failed"
            prog["stage_label"] = "❌ Upload Failed"
            prog["error"] = error_message
            prog["updated_at"] = time.monotonic()

    def get(self, video_id: int) -> Optional[Dict[str, Any]]:
        """Get the current progress snapshot for video_id."""
        return self._progress.get(video_id)

    def get_for_channel(self, channel_id: int) -> Optional[Dict[str, Any]]:
        """Get progress snapshot by channel_id."""
        vid_id = self._channel_map.get(channel_id)
        if vid_id and vid_id in self._progress:
            return self._progress[vid_id]
        # Search values
        for prog in self._progress.values():
            if prog.get("channel_id") == channel_id:
                return prog
        return None

    def update_metadata(self, video_id: int, data: Dict[str, Any]) -> None:
        """Attach arbitrary metadata to the progress snapshot."""
        if video_id in self._progress:
            self._progress[video_id].update(data)
            self._progress[video_id]["updated_at"] = time.monotonic()

    def clear(self, video_id: int) -> None:
        """Clear progress tracker for video_id."""
        self._progress.pop(video_id, None)
        self._history.pop(video_id, None)
        for ch, vid in list(self._channel_map.items()):
            if vid == video_id:
                self._channel_map.pop(ch, None)


# Global singleton instance
upload_tracker = UploadProgressTracker()
