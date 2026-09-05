# Video Processing & Instagram Publisher Telegram Bot (Stage 1)

Stage 1 skeleton for a Telegram bot that will download YouTube videos, process/re-encode them, and publish them to Instagram, hosted on Azure.

Stage 1 sets up:
- Telegram Bot polling skeleton (using `python-telegram-bot` v20+ async API)
- Fail-fast environment variable loading and validation
- Strict access control (unauthorized requests are silently dropped and logged)
- Structured logging (simultaneous stdout stream for Azure App Service and rotating file under `logs/`)
- Health check commands:
  - `/start` - Confirms the bot is running for authorized users
  - `/status` - Diagnostic checks for Telegram API, Instagram Graph API, and Azure Blob Storage reachability

---

## Folder Structure

```
.
├── bot/
│   ├── __init__.py
│   ├── config.py          # Environment variable loading & validation (fails fast)
│   ├── handlers.py        # /start, /status, access control decorator, stubs
│   ├── logger.py          # Structured logging (stdout + rotating file)
│   └── main.py            # Entry point, builds Application and starts polling
├── logs/                  # Application logs (logs/bot.log)
├── storage/
│   ├── downloads/         # Placeholder for Stage 2 (yt-dlp video downloads)
│   ├── processed/         # Placeholder for Stage 3 (ffmpeg processed videos)
│   └── temp/              # Placeholder for temporary files
├── requirements.txt       # Pinned dependencies
├── .env.example           # Environment template
└── README.md              # Documentation and local setup instructions
```

---

## Prerequisites

- **Python 3.10+** (Python 3.11 recommended)
- Telegram account & Bot Token (obtained from [@BotFather](https://t.me/BotFather))
- Your Telegram numeric user ID (can be checked via [@userinfobot](https://t.me/userinfobot))
- Instagram Professional/Business account connected to a Facebook page with Graph API access token
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
# Comma-separated numeric Telegram user IDs allowed to use the bot
ALLOWED_TELEGRAM_USER_IDS=735006720

# Instagram Graph API Configuration
INSTAGRAM_ACCESS_TOKEN=your_instagram_graph_api_long_lived_token_here
INSTAGRAM_BUSINESS_ACCOUNT_ID=your_instagram_business_account_id_here

# Azure Blob Storage Configuration
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=myaccount;AccountKey=mykey;EndpointSuffix=core.windows.net

# Logging (DEBUG, INFO, WARNING, ERROR)
LOG_LEVEL=INFO
```

---

## Running the Bot

Run from the project root directory:

```bash
python -m bot.main
```

Or:

```bash
python bot/main.py
```

---

## Commands

- `/start`:
  - Authorized users receive a confirmation message that the bot is running.
  - Unauthorized users are silently ignored (no response sent; security log entry created).
- `/status`:
  - Runs diagnostics and reports:
    - **Telegram Bot API**: `[PASS]`
    - **Instagram Graph API**: `[PASS]` / `[FAIL]` with account username or error code
    - **Azure Blob Storage**: `[PASS]` / `[FAIL]` with account SKU or error details
    - Summary status line

---

## Logging

- **Console (stdout)**: Unbuffered stdout logging optimized for Azure App Service log stream (`az webapp log tail`).
- **File (`logs/bot.log`)**: Rotating log file with 5 MB maximum size and 5 rotated backups.
- **Log Level**: Controlled via `LOG_LEVEL` environment variable (`DEBUG`, `INFO`, `WARNING`, `ERROR`).
