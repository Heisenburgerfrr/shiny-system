# Video Processing & Instagram Publisher Telegram Bot

A Telegram bot that downloads YouTube videos, processes and re-encodes them with studio-grade anti-fingerprint protection, and publishes them to Instagram, built for deployment on Microsoft Azure.

---

## Current Status: Stage 3 (Memoxz Video Processing via FFmpeg) Complete

- **Stage 1 (Complete)**: Bot skeleton, environment configuration, structured logging, access control, `/start`, and `/status` diagnostics.
- **Stage 2 (Complete)**: YouTube link detection, non-blocking background downloader using `yt_dlp`, persistent SQLite job store (`storage/jobs.db`), live throttled Telegram progress edits, cloud IP workarounds (cookies, player client rotation, proxy support), error classification, and interrupted job auto-recovery.
- **Stage 3 (Complete)**: Memoxz studio-grade video processing engine (from `Wel/main.py`), applying anti-fingerprint visual/acoustic hash breakup, Adobe Premiere Pro CC 2024 metadata injection, CRF 17 visually lossless master quality, 320k AAC audio, real-time render progress bar to Telegram, and automated source cleanup.
- **Stage 4 (Upcoming)**: Cover image handling & Azure Blob upload & Instagram publishing.

---

## Folder Structure

```
.
├── bot/
│   ├── __init__.py
│   ├── config.py          # Environment variable loading & validation
│   ├── db.py              # SQLite job store (storage/jobs.db) & recovery
│   ├── downloader.py      # yt-dlp downloader, retries, and error classification
│   ├── processor.py       # Memoxz FFmpeg engine, anti-fingerprint layer, Premiere metadata
│   ├── handlers.py        # /start, /status, download & processing pipeline handlers
│   ├── logger.py          # Structured logging (stdout + rotating file)
│   └── main.py            # Entry point, polling loop, startup job recovery
├── logs/                  # Application logs (logs/bot.log)
├── storage/
│   ├── downloads/         # Downloaded videos ({job_id}.mp4)
│   ├── processed/         # Final processed videos ({job_id}.mp4)
│   ├── temp/              # Placeholder for temporary working files
│   └── jobs.db            # SQLite job state database
├── tests/
│   ├── test_stage1.py     # Stage 1 verification tests
│   ├── test_stage2.py     # Stage 2 verification tests
│   └── test_stage3.py     # Stage 3 verification tests
├── requirements.txt       # Pinned dependencies
├── .env.example           # Environment template
└── README.md              # Documentation and local setup instructions
```

---

## Video Processing Engine (Memoxz Anti-Fingerprint Layer)

The bot integrates the high-performance Memoxz video processing pipeline:

1. **Visual Hash Breakup (Anti-Fingerprint)**:
   - Dynamic micro-zoom and even-dimension crop (`scale=trunc(iw*crop_factor/2)*2...`) ensuring hardware acceleration speed.
   - Randomized subtle temporal noise (`noise=alls=0.8:allf=t+u`).
   - Micro-shifts across contrast, brightness, saturation, and gamma.
   - Micro-speed shift (`setpts=PTS/speed_factor`).
2. **Acoustic Print Breakup**:
   - Matching audio speed/tempo shift (`atempo=speed_factor`).
   - Dual-band equalizer frequency adjustment (`equalizer=f=100...`, `equalizer=f=2000...`).
   - Volume boost (`volume=1.008`).
3. **Adobe Premiere Pro CC 2024 Metadata Injection**:
   - Strips all original download and platform metadata (`-map_metadata -1`).
   - Injects authentic Adobe Premiere Pro CC 2024 / Adobe Media Encoder tags, creation timestamps, and stream handlers (`VideoHandler`, `SoundHandler`).
4. **Master Quality & Instagram Reels Optimization**:
   - Codec: `-c:v libx264 -preset medium -crf 17` (studio master visual quality).
   - Audio: `-c:a aac -b:a 320k` (high-fidelity audio).
   - Pixel format: `-pix_fmt yuv420p` with `-movflags +faststart` (Instagram Reels specification).
5. **Real-time Progress Streaming**:
   - As FFmpeg renders the video, the Telegram status message displays a live visual progress bar:
     `⚙️ Processing: [████████░░░░░░░░] 50% (14.2s / 28.4s) | CRF 17 Master | 320k AAC`
6. **Automatic Cleanup**:
   - Upon successful render and `ffprobe` verification, the raw downloaded file in `storage/downloads/` is automatically deleted to save disk space.

---

## Prerequisites

- **Python 3.10+** (Python 3.11 recommended)
- **ffmpeg** and **ffprobe** installed and in system PATH
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

## Running the Bot

Run from the project root directory:

```bash
python -m bot.main
```

---

## Running Verification Tests

Run the automated test suites:

```powershell
.\venv\Scripts\python.exe tests/test_stage1.py
.\venv\Scripts\python.exe tests/test_stage2.py
.\venv\Scripts\python.exe tests/test_stage3.py
```
