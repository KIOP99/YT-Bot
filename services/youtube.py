"""
services/youtube.py
--------------------
YouTube Data API v3 integration:
  - OAuth 2.0 flow (authorization URL, token exchange, refresh)
  - Resumable video uploads with progress tracking
  - Thumbnail setting
  - Quota-aware error handling
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from core.config import settings
from core.logging_config import get_logger
from services.encryption import decrypt, encrypt, safe_decrypt

log = get_logger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

# YouTube API quota units
QUOTA_UPLOAD_COST = 1600
QUOTA_THUMBNAIL_COST = 50


class QuotaExceededError(Exception):
    """Raised when YouTube API quota is exceeded (10,000 units/day project cap)."""


class UploadLimitExceededError(Exception):
    """Raised when YouTube daily upload limit for the channel is exceeded (Google 24h channel cap)."""

    def __init__(
        self,
        message: str = "YouTube Daily Upload Limit Exceeded: This channel has reached Google's maximum video uploads for today (24-hour limit).",
        resolution: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.resolution = resolution or (
            "YouTube limits uploads per channel per 24 hours. To resolve this in hosting:\n"
            "1. Unlock 'Advanced features' in YouTube Studio (Settings > Channel > Feature eligibility) via Video Verification or ID to raise daily limits to 100+ uploads/day.\n"
            "2. Switch to another connected YouTube channel in the Channels tab.\n"
            "3. Or wait for Google's 24-hour rolling limit to reset."
        )


class YouTubeService:
    """Manages a single YouTube channel's OAuth credentials and API calls."""

    def __init__(
        self,
        channel_id: int,
        encrypted_access_token: Optional[str],
        encrypted_refresh_token: Optional[str],
        token_expiry: Optional[str],
    ):
        self.channel_id = channel_id
        self._access_token = safe_decrypt(encrypted_access_token)
        self._refresh_token = safe_decrypt(encrypted_refresh_token)
        self._token_expiry = token_expiry
        self._credentials: Optional[Credentials] = None

    def _build_credentials(self) -> Credentials:
        """
        Build Google Credentials object from stored tokens.
        Note: scopes=None is passed so the refresh request does not send an explicit
        scope parameter to Google, which would fail with 'invalid_scope: Bad Request'
        if any scope was not originally granted or is not enabled in the cloud project.
        """
        creds = Credentials(
            token=self._access_token,
            refresh_token=self._refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            scopes=None,
        )
        if self._token_expiry:
            try:
                dt = datetime.fromisoformat(self._token_expiry)
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                creds.expiry = dt
            except ValueError:
                pass
        return creds

    def _refresh_if_needed(self) -> Credentials:
        """Refresh the access token if expired."""
        creds = self._build_credentials()
        if creds.expired or not creds.valid:
            log.info("Refreshing YouTube OAuth token", channel_id=self.channel_id)
            try:
                creds.refresh(GoogleRequest())
            except Exception as exc:
                if "invalid_scope" in str(exc) and getattr(creds, "_scopes", None):
                    log.warning("OAuth refresh failed with invalid_scope; clearing scopes and retrying", error=str(exc))
                    creds._scopes = None
                    creds.refresh(GoogleRequest())
                else:
                    raise
            self._access_token = creds.token
            self._token_expiry = creds.expiry.isoformat() if creds.expiry else None
        return creds

    def get_encrypted_tokens(self) -> dict[str, str]:
        """Return freshly encrypted tokens for DB storage."""
        return {
            "encrypted_access_token": encrypt(self._access_token or ""),
            "encrypted_refresh_token": encrypt(self._refresh_token or ""),
            "token_expiry": self._token_expiry or "",
        }

    def _build_service(self):
        creds = self._refresh_if_needed()
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    # ── OAuth flow helpers ─────────────────────────────────────────────────

    @staticmethod
    def get_authorization_url(state: str) -> str:
        """Return the Google OAuth 2.0 authorization URL."""
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uris": [settings.google_redirect_uri],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=SCOPES,
            redirect_uri=settings.google_redirect_uri,
            autogenerate_code_verifier=False,
        )
        url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state,
        )
        return url

    @staticmethod
    def exchange_code(code: str) -> dict[str, str]:
        """Exchange an OAuth authorization code for tokens directly with Google."""
        from datetime import timedelta
        resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15.0,
        )
        if resp.status_code != 200:
            log.error("Google token exchange failed", status=resp.status_code, body=resp.text)
            raise ValueError(f"Google token exchange failed: {resp.text}")

        data = resp.json()
        expires_in = data.get("expires_in", 3600)
        expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        return {
            "access_token": data.get("access_token", ""),
            "refresh_token": data.get("refresh_token", ""),
            "token_expiry": expiry.isoformat(),
        }

    # ── Channel info ──────────────────────────────────────────────────────

    def get_channel_info(self) -> dict[str, Any]:
        """Fetch the authenticated channel's basic info."""
        svc = self._build_service()
        resp = svc.channels().list(part="snippet,statistics", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            raise ValueError("No YouTube channel found for these credentials")
        item = items[0]
        thumbnails = item["snippet"].get("thumbnails", {})
        thumb_url = (
            thumbnails.get("high", {}).get("url")
            or thumbnails.get("medium", {}).get("url")
            or thumbnails.get("default", {}).get("url")
        )
        return {
            "youtube_channel_id": item["id"],
            "title": item["snippet"]["title"],
            "description": item["snippet"].get("description", ""),
            "subscriber_count": int(item["statistics"].get("subscriberCount", 0)),
            "video_count": int(item["statistics"].get("videoCount", 0)),
            "thumbnail_url": thumb_url,
            "custom_url": item["snippet"].get("customUrl", ""),
        }

    # ── Video upload ──────────────────────────────────────────────────────

    def upload_video(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: list[str],
        category_id: str = "22",
        privacy_status: str = "public",
        scheduled_publish_at: Optional[datetime] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        """
        Upload a video file using resumable upload.
        Returns the YouTube video ID on success.
        """
        svc = self._build_service()

        body: dict[str, Any] = {
            "snippet": {
                "title": title[:100],
                "description": description,
                "tags": tags,
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }

        if scheduled_publish_at:
            body["status"]["privacyStatus"] = "private"
            body["status"]["publishAt"] = scheduled_publish_at.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            )

        media = MediaFileUpload(
            video_path,
            mimetype="video/*",
            resumable=True,
            chunksize=4 * 1024 * 1024,  # 4 MB chunks (fast & reliable)
        )

        insert_request = svc.videos().insert(
            part=",".join(body.keys()),
            body=body,
            media_body=media,
        )

        response = None
        retries = 0
        max_retries = 5

        log.info("Starting YouTube upload", path=video_path, title=title)
        while response is None:
            try:
                status, response = insert_request.next_chunk()
                if status:
                    pct = int(status.progress() * 100) if hasattr(status, "progress") and callable(status.progress) else 0
                    log.info("YouTube upload progress", progress=f"{pct}%")
                    if progress_callback:
                        progress_callback(
                            int(status.resumable_progress),
                            int(status.total_size or 0),
                        )
            except HttpError as e:
                # Extract response content and error text safely
                content_str = ""
                if hasattr(e, "content") and e.content:
                    if isinstance(e.content, bytes):
                        content_str = e.content.decode("utf-8", errors="ignore")
                    else:
                        content_str = str(e.content)
                combined_err = f"{content_str} {str(e)}"

                if "uploadLimitExceeded" in combined_err:
                    log.error("YouTube daily upload limit reached (uploadLimitExceeded)", channel_id=self.channel_id, error=combined_err)
                    raise UploadLimitExceededError(
                        "YouTube Daily Upload Limit Exceeded: This channel has reached Google's maximum video uploads for today (24-hour limit). "
                        "To fix: (1) Enable 'Advanced features' in YouTube Studio (Settings > Channel > Feature eligibility) to unlock 100+ uploads/day, "
                        "(2) Switch to another YouTube channel in the Channels tab, or (3) Wait 24 hours for Google's rolling limit to reset."
                    ) from e

                if "quotaExceeded" in combined_err:
                    log.error("YouTube API quota exceeded (quotaExceeded)", channel_id=self.channel_id, error=combined_err)
                    raise QuotaExceededError("YouTube API quota exceeded (10,000 units/day project cap)") from e

                if e.resp.status in (500, 502, 503, 504) and retries < max_retries:
                    wait = 2 ** retries
                    log.warning("Retrying upload chunk", retry=retries, wait=wait)
                    time.sleep(wait)
                    retries += 1
                else:
                    raise

        video_id: str = response["id"]
        log.info("Upload complete", video_id=video_id)
        return video_id

    def set_thumbnail(self, video_id: str, thumbnail_path: str, max_retries: int = 4) -> bool:
        """
        Set the thumbnail for an uploaded video with retry on YouTube propagation delay.
        Enforces YouTube 2MB max image size and proper MIME type.
        """
        import os
        import time
        import mimetypes
        from pathlib import Path
        from PIL import Image

        abs_path = str(Path(thumbnail_path).resolve())
        if not os.path.exists(abs_path):
            log.error("Thumbnail file does not exist", path=abs_path, video_id=video_id)
            raise FileNotFoundError(f"Thumbnail not found: {abs_path}")

        # Ensure thumbnail is YouTube official working size (1280x720, <= 2MB JPEG)
        try:
            from services.thumbnail import standardize_thumbnail
            standardize_thumbnail(abs_path, target_width=1280, target_height=720)
        except Exception as e:
            log.warning("Could not standardize thumbnail before YouTube API upload", error=str(e))

        mimetype, _ = mimetypes.guess_type(abs_path)
        if not mimetype or mimetype not in ("image/jpeg", "image/png"):
            mimetype = "image/jpeg"

        svc = self._build_service()
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                media = MediaFileUpload(abs_path, mimetype=mimetype, resumable=False)
                svc.thumbnails().set(videoId=video_id, media_body=media).execute()
                log.info("Thumbnail successfully set on YouTube", video_id=video_id, path=abs_path, attempt=attempt)
                return True
            except HttpError as e:
                last_error = e
                err_str = str(e)
                if e.resp.status == 403 and "quotaExceeded" in str(e.content):
                    raise QuotaExceededError("Thumbnail quota exceeded") from e

                # Check for unverified channel permission error
                if e.resp.status in (400, 403) and any(x in err_str for x in ["customThumbnailsNotAllowed", "caller does not have permission", "not enabled"]):
                    log.warning(
                        "YouTube channel is not verified for custom thumbnails (requires phone verification in YouTube Studio). "
                        "Note: Thumbnail has already been embedded directly into the video as the opening frame, which YouTube uses as default thumbnail.",
                        video_id=video_id,
                        error=err_str,
                    )
                    return False

                # 404 Video Not Found is a common race condition immediately after upload
                if e.resp.status == 404 and attempt < max_retries:
                    wait_sec = attempt * 2 + 1  # 3s, 5s, 7s
                    log.info(
                        f"Video {video_id} not yet indexed by thumbnail backend (404). Waiting {wait_sec}s for YouTube propagation (attempt {attempt}/{max_retries})..."
                    )
                    time.sleep(wait_sec)
                    continue

                if e.resp.status in (500, 502, 503) and attempt < max_retries:
                    wait_sec = attempt * 2
                    log.info(f"YouTube thumbnail server error ({e.resp.status}). Retrying in {wait_sec}s...", attempt=attempt)
                    time.sleep(wait_sec)
                    continue

                log.error("Failed to set thumbnail on YouTube", error=err_str, video_id=video_id, attempt=attempt)
                raise
            except Exception as e:
                last_error = e
                log.error("Unexpected error setting thumbnail", error=str(e), video_id=video_id)
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                raise

        if last_error:
            raise last_error
        return False

    # ── Video privacy / release ───────────────────────────────────────────

    def set_privacy(self, video_id: str, privacy_status: str = "public") -> bool:
        """Update a video's privacy status on YouTube (e.g. from private to public)."""
        svc = self._build_service()
        try:
            body = {
                "id": video_id,
                "status": {
                    "privacyStatus": privacy_status,
                },
            }
            svc.videos().update(part="status", body=body).execute()
            log.info("YouTube video privacy updated", video_id=video_id, privacy=privacy_status)
            return True
        except HttpError as e:
            if e.resp.status == 403 and "quotaExceeded" in str(e.content):
                raise QuotaExceededError("YouTube API quota exceeded") from e
            log.error("Failed to update video privacy", video_id=video_id, error=str(e))
            raise

    # ── Video deletion ────────────────────────────────────────────────────

    def delete_video(self, video_id: str) -> bool:
        """
        Delete a video from YouTube by its video ID.
        Returns True on successful deletion (or if video was already deleted).
        """
        svc = self._build_service()
        try:
            svc.videos().delete(id=video_id).execute()
            log.info("YouTube video deleted successfully", video_id=video_id)
            return True
        except HttpError as e:
            if e.resp.status == 404:
                log.info("YouTube video already deleted or not found", video_id=video_id)
                return True
            if e.resp.status == 403 and "quotaExceeded" in str(e.content):
                raise QuotaExceededError("YouTube API quota exceeded") from e
            log.error("Failed to delete YouTube video", video_id=video_id, error=str(e))
            raise

    # ── Video comments ────────────────────────────────────────────────────

    def post_comment(self, video_id: str, text: str) -> dict[str, Any]:
        """
        Post a top-level comment to a YouTube video.
        Requires 'https://www.googleapis.com/auth/youtube.force-ssl' scope.
        """
        svc = self._build_service()
        try:
            body = {
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {
                        "snippet": {
                            "textOriginal": text
                        }
                    }
                }
            }
            resp = svc.commentThreads().insert(part="snippet", body=body).execute()
            comment_id = resp.get("id", "")
            log.info("YouTube comment posted successfully", video_id=video_id, comment_id=comment_id)
            return resp
        except HttpError as e:
            if e.resp.status == 403 and "quotaExceeded" in str(e.content):
                raise QuotaExceededError("YouTube API quota exceeded while posting comment") from e
            log.error("Failed to post YouTube comment", video_id=video_id, error=str(e))
            raise


def build_youtube_service_from_db(channel) -> YouTubeService:
    """Convenience factory from a YouTubeChannel ORM object."""
    return YouTubeService(
        channel_id=channel.id,
        encrypted_access_token=channel.encrypted_access_token,
        encrypted_refresh_token=channel.encrypted_refresh_token,
        token_expiry=channel.token_expiry,
    )
