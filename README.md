# Video Processing & Instagram Publisher Telegram Bot

A Telegram bot that downloads YouTube videos, processes and re-encodes them, and publishes them to Instagram, built for deployment on Microsoft Azure.

---

## Current Status: Stage 2 (YouTube Download via yt-dlp) Complete

- **Stage 1 (Complete)**: Bot skeleton, environment configuration, structured logging, access control, `/start`, and `/status` diagnostics.
- **Stage 2 (Complete)**: YouTube link detection, non-blocking background downloader using `yt_dlp`, persistent SQLite job store (`storage/jobs.db`), live throttled Telegram progress edits, cloud IP workarounds (cookies, player client rotation, proxy support), error classification, and interrupted job auto-recovery.
- **Stage 3 (Upcoming)**: Video processing & aspect ratio conversion (ffmpeg).
- **Stage 4 (Upcoming)**: Azure Blob upload & Instagram publishing.

---

## Folder Structure

```
.
├── bot/
│   ├── __init__.py
│   ├── config.py          # Environment variable loading & validation
│   ├── db.py              # SQLite job store (storage/jobs.db) & recovery
│   ├── downloader.py      # yt-dlp downloader, retries, and error classification
│   ├── handlers.py        # /start, /status, and YouTube URL download handlers
│   ├── logger.py          # Structured logging (stdout + rotating file)
│   └── main.py            # Entry point, polling loop, startup job recovery
├── logs/                  # Application logs (logs/bot.log)
├── storage/
│   ├── downloads/         # Downloaded videos ({job_id}.mp4)
│   ├── processed/         # Placeholder for Stage 3 (ffmpeg processed videos)
│   ├── temp/              # Placeholder for temporary working files
│   └── jobs.db            # SQLite job state database
├── tests/
│   ├── test_stage1.py     # Stage 1 verification tests
│   └── test_stage2.py     # Stage 2 verification tests
├── requirements.txt       # Pinned dependencies
├── .env.example           # Environment template
└── README.md              # Documentation and local setup instructions
```

---

## Prerequisites

- **Python 3.10+** (Python 3.11 recommended)
- **ffmpeg** installed and in system PATH (required for merging best video and audio streams into MP4)
- Telegram account & Bot Token (from [@BotFather](https://t.me/BotFather))
- Telegram numeric user ID (from [@userinfobot](https://t.me/userinfobot))
- Instagram Professional/Business account connected to Facebook Page with Graph API token
- Azure Storage Account connection string

---

## Local Setup

### 1. Set Up Virtual Environment

```bash
# Windows
python -m venv venv
.\venv\Scripts\activate

# Linux / macOS
python3 -m venv venv
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
ALLOWED_TELEGRAM_USER_IDS=735006720

# Instagram Graph API Configuration
INSTAGRAM_ACCESS_TOKEN=your_instagram_graph_api_long_lived_token_here
INSTAGRAM_BUSINESS_ACCOUNT_ID=your_instagram_business_account_id_here

# Azure Blob Storage Configuration
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=myaccount;AccountKey=mykey;EndpointSuffix=core.windows.net

# Logging (DEBUG, INFO, WARNING, ERROR)
LOG_LEVEL=INFO

# Optional: Cloud IP Workarounds for yt-dlp on Azure
# YTDLP_COOKIES_PATH=./cookies.txt
# YTDLP_PROXY_URL=http://user:pass@proxy:8080
# YTDLP_PLAYER_CLIENTS=android,ios,web
```

---

## Cloud IP Workarounds for Azure

When running `yt-dlp` from cloud datacenter IPs (like Azure App Service or VMs), YouTube often triggers bot detection ("Sign in to confirm you're not a bot" or "The page needs to be reloaded").

Stage 2 incorporates 3 workarounds for this:
1. **Cookies file (`YTDLP_COOKIES_PATH`)**:
   - Export cookies from your logged-in browser session into Netscape format (using browser extensions like *Get cookies.txt LOCALLY*).
   - Set `YTDLP_COOKIES_PATH=./cookies.txt` in `.env`.
   - If the file is missing or invalid, the bot logs a warning and proceeds without crashing.
2. **Player Client Fallback Rotation (`YTDLP_PLAYER_CLIENTS`)**:
   - The bot automatically cycles through `android` -> `ios` -> `web` clients before giving up if bot detection occurs.
3. **Residential Proxy (`YTDLP_PROXY_URL`)**:
   - An optional HTTP or SOCKS5 proxy URL can be supplied to route YouTube requests through non-datacenter IPs.

---

## Running the Bot

Run from the project root directory:

```bash
python -m bot.main
```

---

## Bot Usage

- **Download YouTube Video**:
  - Send any YouTube URL (`https://www.youtube.com/watch?v=...`, `https://youtu.be/...`, or `https://www.youtube.com/shorts/...`).
  - The bot immediately sends an acknowledgment with a unique `job_id`.
  - The message dynamically updates with real-time download progress (percentage, download speed, and ETA) throttled to prevent Telegram rate limits.
  - On completion, reports video title, duration, file size, and saves to `storage/downloads/{job_id}.mp4`.
- `/start`: Confirms the bot is operational for authorized users.
- `/status`: Runs diagnostics for Telegram Bot API, Instagram Graph API, and Azure Blob Storage reachability.

---

## Running Verification Tests

Run the automated test suites:

```powershell
# Stage 1 tests (skeleton, access control, status)
.\venv\Scripts\python.exe tests/test_stage1.py

# Stage 2 tests (job store, error classification, regex, downloader, cleanups)
.\venv\Scripts\python.exe tests/test_stage2.py
```
