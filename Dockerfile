# ═══════════════════════════════════════════════════════════════════
#  Dockerfile — YTBot Dashboard
# ═══════════════════════════════════════════════════════════════════

FROM python:3.11-slim AS base

# System dependencies: FFmpeg + build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Install Python dependencies (cacheable layer) ────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Copy application code ───────────────────────────────────────────
COPY . .

# ── Create non-root user for security ──────────────────────────────
RUN useradd -r -s /bin/false ytbot && \
    mkdir -p uploads/videos uploads/thumbnails uploads/processed uploads/previews logs && \
    chown -R ytbot:ytbot /app

USER ytbot

# ── Expose port ─────────────────────────────────────────────────────
EXPOSE 19232 8000

# ── Default: run bot + API together ─────────────────────────────────
CMD ["python", "run_bot.py"]
