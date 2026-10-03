"""
api/middleware.py
-----------------
Custom middleware:
  - CSRF protection (double-submit cookie pattern)
  - Rate limiting (via slowapi)
  - Request ID injection for tracing
  - GZip compression
"""

from __future__ import annotations

import time
import uuid
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from slowapi import Limiter
from slowapi.util import get_remote_address

from core.config import settings
from core.logging_config import get_logger

log = get_logger(__name__)

# Rate limiter instance — attach to app via app.state.limiter
limiter = Limiter(key_func=get_remote_address)

# Paths that are exempt from CSRF checks
CSRF_EXEMPT_PATHS = {
    "/api/channels/oauth/callback",
    "/auth/login",
    "/auth/logout",
    "/api/bots",
}
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def add_middleware(app: FastAPI) -> None:
    """Register all middleware on the FastAPI app."""

    # CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            settings.app_base_url.rstrip("/"),
            "http://og.yaddu.net:19232",
            "http://localhost:19232",
            "http://127.0.0.1:19232",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ],
        allow_origin_regex=r"^https?://.*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # GZip compression
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Callable) -> Response:
        """Inject a unique X-Request-ID header for tracing."""
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.middleware("http")
    async def logging_middleware(request: Request, call_next: Callable) -> Response:
        """Log each request with timing."""
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        log.info(
            "HTTP",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
            request_id=getattr(request.state, "request_id", "-"),
        )
        return response

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next: Callable) -> Response:
        """
        Double-submit CSRF protection for state-changing endpoints.
        - The CSRF token is stored in a cookie (`csrf_token`).
        - The client can echo it via the `X-CSRF-Token` header or `csrf_token` form field.
        """
        if request.method in CSRF_SAFE_METHODS:
            return await call_next(request)

        path = request.url.path
        if path in CSRF_EXEMPT_PATHS or path.startswith("/static") or path.startswith("/api/bots"):
            return await call_next(request)

        # Skip CSRF for API token auth (Authorization header present)
        if request.headers.get("Authorization"):
            return await call_next(request)

        cookie_token = request.cookies.get("csrf_token", "")
        header_token = request.headers.get("X-CSRF-Token", "")

        # Also support form body submission for traditional HTML forms
        if not header_token and "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
            import urllib.parse
            body = await request.body()
            async def receive():
                return {"type": "http.request", "body": body}
            request._receive = receive
            parsed = urllib.parse.parse_qs(body.decode("utf-8", errors="ignore"))
            header_token = parsed.get("csrf_token", [""])[0]

        if not cookie_token or not header_token:
            log.warning("CSRF token missing", path=path, method=request.method)
            return Response(content="CSRF token missing", status_code=403)

        import hmac
        if not hmac.compare_digest(cookie_token, header_token):
            log.warning("CSRF token mismatch", path=path)
            return Response(content="CSRF validation failed", status_code=403)

        return await call_next(request)

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next: Callable) -> Response:
        """Add security headers to all responses."""
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response
