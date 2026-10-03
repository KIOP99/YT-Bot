"""
run_api.py
----------
Entrypoint for the FastAPI web server.
The bot and API share the same asyncio event loop when run together.
"""

import asyncio
import uvicorn

from core.config import settings
from core.logging_config import setup_logging

if __name__ == "__main__":
    setup_logging()
    uvicorn.run(
        "api.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_env == "development",
        log_level=settings.log_level.lower(),
        access_log=False,  # handled by our middleware
    )
