# 📹 YTBot Dashboard

> **Production-ready YouTube upload automation** with a premium web dashboard and Discord bot.  
> Upload once. Schedule daily. Rotate thumbnails. Announce automatically.

---

## ✨ Features at a Glance

| Feature | Details |
|---|---|
| **Dashboard** | Dark-mode, glassmorphism UI with countdown timers, stats, drag-and-drop |
| **Security** | Argon2id hashing, JWT cookies, CSRF protection, rate limiting, optional TOTP 2FA |
| **Credential Rotation** | Auto-rotates admin username/password, delivers via Discord DM |
| **Video Management** | Chunked upload with progress bar, ffprobe metadata, preview player |
| **Thumbnail Pool** | Drag-and-drop reorder, Pillow validation, sequential or random rotation |
| **Scheduling** | APScheduler with randomized window, 30-min lead processing, retry with backoff |
| **YouTube OAuth** | Secure OAuth 2.0 only — encrypted refresh tokens via Fernet AES |
| **Discord Bot** | Slash commands: `/status`, `/next-upload`, `/pause`, `/resume`, `/force-upload` |
| **Multi-Channel** | Per-channel video, thumbnail pool, schedule, and Discord settings |
| **Docker** | Single `docker-compose up` deployment with Postgres |

---

## 🚀 Quick Start (5 Steps)

### Step 1 — Clone & Configure

```bash
git clone <your-repo> ytbot
cd ytbot
cp .env.example .env
```

Open `.env` and fill in:
- `APP_SECRET_KEY` — generate with `python -c "import secrets; print(secrets.token_hex(32))"`
- `JWT_SECRET` — same method, separate value
- `FERNET_KEY` — generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` — initial login credentials
- `DISCORD_BOT_TOKEN` — see Step 2
- `DISCORD_ADMIN_USER_ID`, `DISCORD_GUILD_ID`, `DISCORD_ANNOUNCEMENT_CHANNEL_ID`
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — see Step 3

---

### Step 2 — Create the Discord Bot

1. Go to [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. **New Application** → name it "YTBot"
3. **Bot** tab → **Add Bot** → copy the **Token** → paste into `.env` as `DISCORD_BOT_TOKEN`
4. Enable **Privileged Gateway Intents**: `Message Content Intent`, `Server Members Intent`
5. **OAuth2 → URL Generator** → scopes: `bot`, `applications.commands`; permissions: `Send Messages`, `Embed Links`, `Attach Files` → invite to your server
6. Copy your **User ID** (Settings → Advanced → Developer Mode, then right-click your name) → `DISCORD_ADMIN_USER_ID`
7. Right-click your server → Copy Server ID → `DISCORD_GUILD_ID`
8. Right-click the announcement channel → Copy ID → `DISCORD_ANNOUNCEMENT_CHANNEL_ID`

---

### Step 3 — Google Cloud OAuth Setup

1. Go to [https://console.cloud.google.com](https://console.cloud.google.com)
2. Create a project (or select existing)
3. **APIs & Services → Enable APIs**: enable **YouTube Data API v3**
4. **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
   - Application type: **Web application**
   - Authorized redirect URIs: `http://localhost:8000/api/channels/oauth/callback`
   - For production: `https://yourdomain.com/api/channels/oauth/callback`
5. Copy **Client ID** → `GOOGLE_CLIENT_ID` and **Client Secret** → `GOOGLE_CLIENT_SECRET`
6. **APIs & Services → OAuth consent screen** — fill in app name, contact email, add scope `youtube.upload`

> ⚠️ **Important**: YouTube uploads require the OAuth consent screen to be verified by Google if your app is public. For personal use, add yourself as a test user.

---

### Step 4 — Install & Run

#### Option A: Docker (Recommended)

```bash
docker-compose up -d --build
```

The dashboard will be available at `http://localhost:8000`

#### Option B: Local Python

```bash
# Create virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

# Install dependencies
pip install -r requirements.txt

# Install FFmpeg
# Windows: winget install ffmpeg  or  choco install ffmpeg
# Ubuntu: sudo apt install ffmpeg
# macOS: brew install ffmpeg

# Run (bot + API together)
python run_bot.py

# Or run them separately:
python run_api.py               # API only
python -c "import asyncio; from bot.client import bot; from core.config import settings; asyncio.run(bot.start(settings.discord_bot_token))"
```

---

### Step 5 — First-Time Setup

1. Open `http://localhost:8000` → log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`
2. **Channels** → "Add & Authorize" → complete the Google OAuth flow
3. **Videos** → upload your video, set title/description/tags
4. **Thumbnails** → upload at least one thumbnail (1280×720 minimum, 16:9)
5. **Schedule** → configure timezone and upload window → **Save**
6. **Discord** → enter Discord channel ID and customize the announcement template
7. **Done!** The bot will upload every 24 hours automatically.

---

## 📁 Project Structure

```
ytbot/
├── bot/          # Discord bot (discord.py)
│   └── cogs/     # Slash commands
├── api/          # FastAPI application
│   └── routers/  # Route handlers (auth, videos, thumbnails, etc.)
├── scheduler/    # APScheduler jobs and engine
├── services/     # Business logic (YouTube, FFmpeg, thumbnails, notifications)
├── models/       # SQLAlchemy ORM models
├── frontend/     # Jinja2 templates + static CSS/JS
├── core/         # Config, database, security utilities
├── tests/        # Pytest unit tests
└── uploads/      # Video and thumbnail storage (volume-mounted)
```

---

## 🛡️ Security Model

| Mechanism | Implementation |
|---|---|
| Password hashing | Argon2id (memory-hard, 64 MB, 2 iterations) |
| Session tokens | JWT (HS256) in HTTP-only cookies |
| CSRF protection | Double-submit cookie pattern |
| Rate limiting | slowapi (5 login attempts/minute per IP) |
| OAuth tokens | Fernet AES-128-CBC encrypted at rest |
| Credential rotation | Auto-generated via `secrets.token_urlsafe()`, delivered via DM |
| 2FA | TOTP via pyotp (optional) |
| Security headers | CSP, X-Frame-Options, HSTS, nosniff |

---

## 🤖 Discord Slash Commands

| Command | Description | Permission |
|---|---|---|
| `/status` | Show bot latency, scheduler status, next upload times | Admin only |
| `/next-upload` | List next scheduled upload for all channels | Admin only |
| `/pause <channel_id>` | Pause uploads for a channel | Admin only |
| `/resume <channel_id>` | Resume uploads for a channel | Admin only |
| `/force-upload <channel_id>` | Trigger an immediate upload | Admin only |

---

## 📊 YouTube API Quota

The YouTube Data API v3 has a daily quota of **10,000 units**.

| Operation | Quota Cost |
|---|---|
| Video upload | 1,600 units |
| Set thumbnail | 50 units |
| **Total per upload** | **~1,650 units** |

At 1 upload/day you use **~1,650 / 10,000 units** (16.5%). Quota resets daily at midnight Pacific Time. Quota exceeded errors are surfaced in the dashboard and alerted via Discord DM.

---

## ⚙️ Configuration Reference

All settings are in `.env`. Key variables:

```env
UPLOAD_WINDOW_START=14    # Earliest hour for uploads (24h, your timezone)
UPLOAD_WINDOW_END=20      # Latest hour for uploads
SCHEDULER_TIMEZONE=US/Eastern
PROCESSING_LEAD_MINUTES=30  # FFmpeg starts this many minutes before upload
CRED_ROTATION_INTERVAL_HOURS=168  # How often to rotate dashboard credentials
TOTP_ENABLED=false  # Set to true to require 2FA after setup
```

---

## 🔧 Troubleshooting

### Bot not coming online
- Verify `DISCORD_BOT_TOKEN` is correct
- Ensure "Message Content Intent" is enabled in the Developer Portal

### YouTube upload fails with 403
- Your OAuth consent screen may need verification — add yourself as a Test User
- Check quota at [console.cloud.google.com/apis/api/youtube.googleapis.com/quotas](https://console.cloud.google.com/apis/api/youtube.googleapis.com/quotas)

### "Thumbnail validation failed"
- Image must be at least **1280×720** pixels
- Aspect ratio must be **16:9** (±5%)
- Formats: JPEG or PNG only

### Scheduler not running
- Check logs at `/security/logs` or `logs/ytbot.log`
- Ensure the channel is marked "Authorized" in the Channels page
- Make sure a video and at least one thumbnail are configured

### Running behind a reverse proxy
- Set `APP_BASE_URL` to your public HTTPS URL
- Set `GOOGLE_REDIRECT_URI` to `https://yourdomain.com/api/channels/oauth/callback`
- Update Authorized Redirect URIs in Google Cloud Console

---

## 🐳 Production Deployment Notes

1. Use **PostgreSQL** — update `DATABASE_URL` in `.env`
2. Set `APP_ENV=production` to disable API docs and enable HSTS
3. Put **Nginx** in front for TLS termination (uncomment in `docker-compose.yml`)
4. Use `alembic upgrade head` for DB migrations instead of `create_all`
5. Set `MAX_VIDEO_SIZE_MB` appropriately for your disk space

---

## 📜 License

MIT — free for personal and commercial use.
