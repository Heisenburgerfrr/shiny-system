# AGENTS.md — YouTube to Instagram Reels Publisher Bot

> **Notice for AI Agents & Developers**: This document contains the complete context, architectural breakdown, operational state, and developer workflows for this repository. Read this file first in any new session to understand the codebase, current branch differences, and environment configurations.

---

## 1. Project Overview & Core Purpose

This repository contains an autonomous, server-grade **Telegram Bot** that ingests video links from **YouTube** (Shorts, regular videos) and **Instagram** (Reels, Carousels/posts), processes them through a **studio-grade anti-fingerprint video processing pipeline**, and publishes them directly to **Instagram Reels** using the official **Meta Instagram Graph API v21.0**. Media is hosted and staged ephemerally using **Microsoft Azure Blob Storage**.

### Why This Project Exists
1. **Bypass Duplicate Content Detection & Shadowbans**: Social media platforms scan visual and acoustic fingerprints to detect re-uploaded videos. This bot applies dynamic, imperceptible visual micro-shifts, temporal noise, audio tempo/EQ adjustments, and strips platform metadata.
2. **Metadata Spoofing**: Injects authentic **Adobe Premiere Pro CC 2024 / Adobe Media Encoder** creation metadata, stream handlers (`VideoHandler`, `SoundHandler`), and encoding tags so algorithms view the video as a freshly rendered project from an editing suite.
3. **Zero-Touch Automation**: A user pastes a YouTube or Instagram URL into Telegram; the bot handles extraction, rendering, cloud upload, Reels container creation, transcoding status polling, and publishing, then returns the live Instagram permalink.

---

## 2. Multi-Account & Branch Strategy

The codebase is structured to run **two separate bots for two different Instagram accounts** on the same Azure virtual machine without conflict:

| Setting | Account 1 (Primary) | Account 2 (Secondary - Current Branch) |
| :--- | :--- | :--- |
| **Git Branch** | `main` | `uns` |
| **Instagram Account** | `@memoxz100k` | `@unshockabl` |
| **Telegram Bot** | `@memoxz_bot` | `@EmergingAI_bot` (ID: `7212575064`) |
| **VM Working Directory** | `/home/azureuser/Memoxz` | `/home/azureuser/unshockabl` |
| **Systemd Service** | `memoxz.service` | `unshockabl.service` |
| **Azure Blob Container** | `ig-uploads` | `ig-uploads` (or `unshockabl-uploads`) |
| **Active Status** | Production | Ready for deployment |

> [!IMPORTANT]
> When working on branch `uns`, do NOT merge branding or service names into `main` without checking. `main` runs `memoxz.service` and `uns` runs `unshockabl.service`.

---

## 3. Current Live Verification Status (Branch: `uns`)

- **Telegram Bot API**: Connected as `@EmergingAI_bot` (`Emerging AI`). Menu commands, long description, and short descriptions are registered and active.
- **Instagram Graph API**: **Verified & Connected** to `@unshockabl` (Account ID: `17841440665968433`) using a permanent Meta Business System User access token.
- **Azure Blob Storage**: **Verified & Reachable** (`memoxz.blob.core.windows.net`, Container `ig-uploads` / `Standard_LRS`).
- **Video Engine (FFmpeg)**: Configured with CRF 17 master quality, 320k AAC, anti-fingerprint filters, and Premiere Pro metadata.
- **Render Concurrency**: Enforced at `max_concurrent_renders = 1` via `AsyncRenderQueue` to prevent CPU lockups.
- **Photo & Carousel Publishing**: Supported for single image/meme posts, 2–10 image/video carousels, and mixed media with automated aspect-ratio padding (matching slide 1's ratio) and Pillow RGB optimization.
- **Caption System**: Defaulted to OFF (preserves original post title & caption from Instagram/YouTube); when toggled ON, uses custom template/suffix.

---

## 4. Architectural Pipeline Flow

```mermaid
flowchart TD
    User([Telegram User]) -->|Sends YouTube or Instagram Link| Handlers[bot/handlers.py]
    Handlers -->|Validates User ID| AccessCheck{Allowed ID?}
    AccessCheck -- No --> SilentIgnore[Drop Request & Log Warning]
    AccessCheck -- Yes --> Downloader[bot/downloader.py yt-dlp]
    
    Downloader -->|Uses cookies.txt & player clients| RawVideo[(storage/downloads/job_id.mp4)]
    RawVideo --> RenderQueue[bot/render_queue.py FIFO Slot]
    
    RenderQueue --> VideoProcessor[bot/processor.py FFmpeg]
    VideoProcessor -->|Visual/Audio Hash Breakup + Premiere Metadata| ProcessedVideo[(storage/processed/job_id.mp4)]
    VideoProcessor -->|Cleanup| DeleteRaw[Auto-delete storage/downloads/]
    
    ProcessedVideo --> AzureManager[bot/azure_storage.py]
    AzureManager -->|Chunked Upload + SAS Token| AzureBlob[(Azure Blob Storage)]
    
    AzureBlob --> IGPublisher[bot/instagram_publish.py]
    IGPublisher -->|1. Create Reels Container| MetaAPI[Meta Graph API v21.0]
    IGPublisher -->|2. Poll Transcoding Status| MetaAPI
    IGPublisher -->|3. Publish Media| MetaAPI
    
    MetaAPI -->|Returns live permalink| Handlers
    Handlers -->|Sends link to user| User
    
    IGPublisher -->|Cleanup| AzureClean[Delete temporary video blob]
    IGPublisher -->|Cleanup| StorageClean[Auto-delete storage/processed/]
```

---

## 5. File & Directory Reference

```
.
├── AGENTS.md                  # Comprehensive AI agent & developer knowledge base (this file)
├── README.md                  # Project overview & local run instructions
├── setup_vm.sh                # 1-click Linux VM installation script (FFmpeg, Deno, Swap, Systemd)
├── unshockabl.service         # Systemd service unit for the second bot (branch: uns)
├── requirements.txt           # Pinned Python package dependencies
├── .env                       # Active runtime environment credentials (gitignored)
├── .env.example               # Template environment configuration
├── cover.jpg                  # Default 9:16 cover image for Instagram Reels
│
├── bot/                       # Core application package
│   ├── __init__.py            # Package initializer
│   ├── config.py              # Strict environment loader, path validation, Deno binary injection
│   ├── db.py                  # SQLite persistent job store (storage/jobs.db) with automatic migration
│   ├── downloader.py          # yt-dlp wrapper with fallback clients, cookie rotation, error categorization
│   ├── processor.py           # Memoxz/Unshockabl FFmpeg engine: anti-fingerprint filters & Premiere metadata
│   ├── render_queue.py        # Async semaphore queue serializing FFmpeg renders (concurrency = 1)
│   ├── cover.py               # PIL inspection & cropping ensuring 9:16 Instagram Reels compliance
│   ├── azure_storage.py       # Blob client, chunked upload, SAS URL generator, public reachability verify
│   ├── instagram_publish.py   # Meta Graph API v21.0 Reels publisher, status poller, rate limiter
│   ├── handlers.py            # Telegram command & message handlers, inline keyboard callbacks, UI throttler
│   ├── recovery.py            # Startup crash recovery scan, active task registry, orphaned file sweeps
│   ├── logger.py              # Structured logging (stdout + rotating log file)
│   └── main.py                # Main entrypoint, application builder, post_init hooks, polling loop
│
├── logs/                      # Log directory (bot.log, systemd_stdout.log)
└── storage/                   # File storage root
    ├── downloads/             # Temporary raw downloads from yt-dlp ({job_id}.mp4)
    ├── processed/             # Rendered anti-fingerprinted videos ({job_id}.mp4)
    ├── temp/                  # Scratch space
    └── jobs.db                # SQLite database tracking job states & user settings
```

---

## 6. Video Processing Engine Specs (`bot/processor.py`)

The bot applies mathematical shifts on every render so no two videos share the same visual or acoustic hash:

1. **Visual Hash Alterations**:
   - Dynamic micro-zoom and even-dimension crop: `scale=trunc(iw*crop_factor/2)*2:trunc(ih*crop_factor/2)*2` (crop factor `1.012` to `1.020`). Even dimensions preserve hardware acceleration speed.
   - Temporal noise injection: `noise=alls=0.8:allf=t+u`.
   - Subtle randomized color grading: Contrast (`1.01`–`1.02`), Brightness (`0.003`–`0.008`), Saturation (`1.01`–`1.025`), Gamma (`1.005`–`1.015`).
   - Micro-speed shift: `setpts=PTS/speed_factor` (`1.006` to `1.014`).
2. **Acoustic Hash Alterations**:
   - Matching audio speed/tempo shift: `atempo=speed_factor`.
   - Dual-band frequency EQ adjustment: `equalizer=f=100:width_type=h:width=50:g=0.8` (bass shift) and `equalizer=f=2000:width_type=h:width=1000:g=0.6` (vocal/presence shift).
   - Micro-volume boost: `volume=1.008`.
3. **Adobe Premiere Pro CC 2024 Metadata Injection**:
   - Strips existing platform metadata: `-map_metadata -1`.
   - Injects authentic tags:
     - `encoder=Adobe Premiere Pro CC 2024 (Windows)`
     - `comment=Rendered in Adobe Premiere Pro CC`
     - `-metadata:s:v:0 handler_name=VideoHandler`
     - `-metadata:s:a:0 handler_name=SoundHandler`
4. **Master Quality Encoding**:
   - Video: `libx264`, `-crf 17`, `-preset medium`, `-pix_fmt yuv420p`, `-movflags +faststart`.
   - Audio: `aac`, `-b:a 320k`.

---

## 7. Telegram Bot Commands & Interactions

| Command | Action | Description |
| :--- | :--- | :--- |
| `/start` | Welcome Screen | Explains how to use the bot and lists supported link types. |
| `/status` | Diagnostic Health Check | Tests Telegram, Instagram Graph API (`_check_instagram`), Azure Blob reachability, disk space, and queue status. |
| `/jobs` | Active Job Monitor | Lists running downloads, queued renders, and publishing jobs. |
| `/job <id>` | Job Detail View | Detailed status of a specific job ID. |
| `/cancel <id>` | Task Cancellation | Aborts an active download or rendering job immediately. |
| `/retry <id>` | Task Retry | Restarts a failed pipeline from the beginning. |
| `/caption` | Caption Template Editor | Displays or updates default caption template, and toggles custom caption ON/OFF (default: OFF, using the post's original caption from YouTube/Instagram). |
| `/cover` | Reels Cover Manager | Previews current cover image, validates 9:16 specs, and toggles custom cover ON/OFF (default: OFF, using native video thumbnail). |
| `/cookies` | YouTube Cookie Updater | Displays instructions or accepts uploaded `cookies.txt` to bypass YouTube bot detection. |

---

## 8. Environment Variables (`.env`)

```ini
# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=7212575064:AAHEvvSB8n_...     # Bot token from @BotFather
ALLOWED_TELEGRAM_USERS=735006720                 # Comma-separated numeric Telegram User IDs

# Instagram Graph API Configuration
INSTAGRAM_ACCOUNT_ID=17841440665968433           # Instagram Business Account ID
INSTAGRAM_ACCESS_TOKEN=EAAO9ZCzCs83k...          # Permanent Meta Business System User Token

# Azure Blob Storage Configuration
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=memoxz;AccountKey=...;EndpointSuffix=core.windows.net
AZURE_BLOB_CONTAINER=ig-uploads                  # Dedicated container for video & cover staging

# Optional Parameters
LOG_LEVEL=INFO                                   # DEBUG, INFO, WARNING, ERROR
# YTDLP_COOKIES_PATH=./cookies.txt               # Path to Netscape cookies.txt
# YTDLP_PROXY_URL=http://user:pass@proxy:8080    # Optional residential proxy for yt-dlp
# YTDLP_PLAYER_CLIENTS=default,ios,mweb,web,android
# DEFAULT_COVER_PATH=./cover.jpg
# INSTAGRAM_CAPTION_SUFFIX=\n\nFollow @unshockabl for daily reels! 🔥
# INSTAGRAM_PUBLISH_RATE_LIMIT_24H=50
```

---

## 9. Deployment on Azure Linux VM (Dual-Bot Setup)

To run both `memoxz` and `unshockabl` simultaneously on the same virtual machine:

### 1. File Structure on Host
```
/home/azureuser/
├── Memoxz/          # git branch: main (runs memoxz.service)
└── unshockabl/      # git branch: uns (runs unshockabl.service)
```

### 2. Setup Commands for `unshockabl` on Host
```bash
# 1. Clone into separate directory
cd /home/azureuser
git clone https://github.com/Heisenburgerfrr/shiny-system.git unshockabl
cd unshockabl
git checkout uns

# 2. Virtual environment & packages
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
mkdir -p storage/downloads storage/processed storage/temp logs

# 3. Setup .env with unshockabl credentials
nano .env

# 4. Install & Start Systemd Service
sudo cp unshockabl.service /etc/systemd/system/unshockabl.service
sudo systemctl daemon-reload
sudo systemctl enable --now unshockabl

# 5. Check status of both services
sudo systemctl status unshockabl
sudo systemctl status memoxz
```

### 3. Fast 1-Click Update on Host
Whenever new code is pushed to branch `uns`:
```bash
cd /home/azureuser/unshockabl
chmod +x update.sh
./update.sh
# Or specify branch explicitly:
./update.sh -b uns
```
This automatically pulls from the git branch, updates dependencies, and restarts `unshockabl.service`.


---

## 10. Important Operational Gotchas

1. **Meta Graph API Tokens**:
   - Never use standard User Access Tokens from Graph API Explorer (they expire in 1–2 hours or 60 days).
   - Always use a **Meta Business System User Token** set to **Never** expire with `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`.
2. **YouTube Datacenter IP Blocks**:
   - Azure VM IPs often trigger `"Sign in to confirm you're not a bot"`.
   - The bot has Deno installed for JS challenge solving and supports uploading a fresh `cookies.txt` directly via Telegram `/cookies` or document upload.
3. **VM Resource Protection**:
   - FFmpeg rendering is heavily CPU/RAM intensive. Always ensure:
     - 4 GB swap space is active (`sudo swapon --show`).
     - `AsyncRenderQueue` maintains `max_concurrent_renders = 1`.
4. **Storage Retention**:
   - Local downloads are purged as soon as processing completes.
   - Azure video blobs have short-lived SAS URLs (4h expiry) and are automatically deleted immediately after publishing, or during startup sweeps.
