"""
api/templates.py
-----------------
Shared Jinja2Templates instance with Python 3.14-compatible settings.
Supports both legacy (name, context) and modern (request, name, context) signatures.
"""

from __future__ import annotations

from typing import Any
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request

# Use cache_size=0 to bypass the Jinja2 LRUCache which breaks on Python 3.14
# due to unhashable dict keys in the cache key tuple.
_env = Environment(
    loader=FileSystemLoader("frontend/templates"),
    autoescape=select_autoescape(["html"]),
    auto_reload=True,
    cache_size=0,  # disable template caching (avoids Python 3.14 hash bug)
)


class RobustJinja2Templates(Jinja2Templates):
    """
    Jinja2Templates wrapper that accepts both the legacy Starlette signature:
        TemplateResponse("template.html", {"request": request, ...}, status_code=200)
    and the modern Starlette 0.28+ signature:
        TemplateResponse(request, "template.html", {...}, status_code=200)
    """

    def TemplateResponse(self, *args: Any, **kwargs: Any) -> Any:
        # Case 1: Legacy call: TemplateResponse(name: str, context: dict, ...)
        if args and isinstance(args[0], str):
            name = args[0]
            context = args[1] if len(args) > 1 else kwargs.pop("context", {})
            request = kwargs.pop("request", None)
            if request is None and isinstance(context, dict):
                request = context.get("request")
            status_code = args[2] if len(args) > 2 else kwargs.pop("status_code", 200)
            return super().TemplateResponse(
                request=request,
                name=name,
                context=context,
                status_code=status_code,
                **kwargs,
            )

        # Case 2: Keyword call with name as string: TemplateResponse(name="page.html", ...)
        if "name" in kwargs and "request" not in kwargs:
            context = kwargs.get("context", {})
            request = context.get("request") if isinstance(context, dict) else None
            return super().TemplateResponse(request=request, **kwargs)

        # Case 3: Modern signature: TemplateResponse(request, name, context, ...)
        return super().TemplateResponse(*args, **kwargs)


templates = RobustJinja2Templates(env=_env)
