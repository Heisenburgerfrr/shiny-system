"""Telegram command handlers, access control, diagnostic checks, and video download handlers."""

import asyncio
import functools
import logging
import re
import time
import uuid
from typing import Callable, Optional, Tuple

import httpx
from azure.storage.blob import BlobServiceClient
from telegram import Message, Update
from telegram.ext import ContextTypes

from bot.config import config
from bot.cover import inspect_cover_image
from bot.db import job_store
from bot.downloader import DownloadResult, _format_bytes, _format_seconds, downloader
from bot.processor import ProcessedResult, video_processor

logger = logging.getLogger("bot.handlers")

# Regex to detect YouTube URLs (videos, shorts, youtu.be, live streams)
YOUTUBE_URL_REGEX = re.compile(
    r"(https?://(?:www\.)?(?:youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/|embed/)|youtu\.be/)[a-zA-Z0-9_\-]+[^\s]*)",
    re.IGNORECASE,
)


def restricted(func: Callable) -> Callable:
    """
    Decorator to restrict handler execution to users in ALLOWED_TELEGRAM_USER_IDS.
    Unauthorized requests are silently ignored (no response sent) and logged as warnings.
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        user_id = user.id if user else None
        username = user.username if user else "unknown"

        if user_id is None or user_id not in config.allowed_telegram_user_ids:
            logger.warning(
                "Unauthorized access attempt blocked: user_id=%s, username=@%s",
                user_id,
                username,
            )
            # Silently ignore - do not reply to unauthorized users
            return

        return await func(update, context, *args, **kwargs)

    return wrapper


async def _check_instagram() -> Tuple[bool, str]:
    """
    Validates Instagram Graph API credentials via a lightweight account inspection call.
    Returns (success: bool, message: str).
    """
    url = f"https://graph.facebook.com/v20.0/{config.instagram_business_account_id}"
    params = {
        "fields": "id,username",
        "access_token": config.instagram_access_token,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)

        if resp.status_code == 200:
            data = resp.json()
            username = data.get("username", "N/A")
            return True, f"Connected (@{username})"
        else:
            try:
                err_data = resp.json().get("error", {})
                err_msg = err_data.get("message", resp.text[:100])
            except Exception:
                err_msg = resp.text[:100]
            logger.error("Instagram Graph API check failed: HTTP %s: %s", resp.status_code, err_msg)
            return False, f"Failed (HTTP {resp.status_code}: {err_msg})"

    except Exception as exc:
        logger.error("Instagram check encountered exception: %s", exc, exc_info=True)
        return False, f"Error ({type(exc).__name__}: {str(exc)[:100]})"


def _check_azure_sync() -> Tuple[bool, str]:
    """
    Synchronously verifies Azure Blob Storage reachability using the connection string.
    """
    try:
        blob_service_client = BlobServiceClient.from_connection_string(
            config.azure_storage_connection_string,
            connection_timeout=5,
            read_timeout=5,
            retry_total=0,
        )
        # Attempt a lightweight account info call with explicit timeout
        account_info = blob_service_client.get_account_information(timeout=5)
        sku_name = account_info.get("sku_name", "standard")
        return True, f"Reachable (Account SKU: {sku_name})"
    except Exception as exc:
        logger.error("Azure Blob Storage check failed: %s", exc, exc_info=True)
        return False, f"Error ({type(exc).__name__}: {str(exc)[:100]})"


async def _check_azure() -> Tuple[bool, str]:
    """Runs Azure Blob Storage verification in a background thread to avoid blocking."""
    return await asyncio.to_thread(_check_azure_sync)


@restricted
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command for authorized users."""
    user = update.effective_user
    logger.info("Received /start command from user_id=%s (@%s)", user.id, user.username)

    welcome_message = (
        "🤖 **Bot is online and operational.**\n\n"
        "Welcome! You are authorized to use this bot.\n\n"
        "Available commands & features:\n"
        "• Send any **YouTube link** to download the video\n"
        "• `/status` - Verify external connections (Instagram, Azure, Telegram)"
    )
    if update.effective_message:
        await update.effective_message.reply_text(welcome_message, parse_mode="Markdown")


@restricted
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles the /status command.
    Checks reachability of Telegram, Instagram Graph API, and Azure Blob Storage.
    """
    user = update.effective_user
    logger.info("Running /status diagnostic check requested by user_id=%s (@%s)", user.id, user.username)

    status_msg = None
    if update.effective_message:
        status_msg = await update.effective_message.reply_text("⏳ Running diagnostic checks...")

    # 1. Telegram check (implicit pass because this handler was invoked)
    tg_status = "[PASS] Operational"

    # 2. Instagram Graph API check
    ig_ok, ig_detail = await _check_instagram()
    ig_status = f"[PASS] {ig_detail}" if ig_ok else f"[FAIL] {ig_detail}"

    # 3. Azure Blob Storage check
    azure_ok, azure_detail = await _check_azure()
    azure_status = f"[PASS] {azure_detail}" if azure_ok else f"[FAIL] {azure_detail}"

    # 4. Default Cover Image check (Stage 4)
    cover_res = inspect_cover_image(config.default_cover_path)
    cover_status = f"[PASS] {cover_res.details}" if cover_res.is_valid else f"[FAIL] {cover_res.details}"

    overall_ok = ig_ok and azure_ok and cover_res.is_valid
    summary = "✅ All systems operational." if overall_ok else "⚠️ One or more checks failed. Review logs."

    report = (
        f"📊 **System Status Report**\n\n"
        f"• **Telegram Bot API**: `{tg_status}`\n"
        f"• **Instagram Graph API**: `{ig_status}`\n"
        f"• **Azure Blob Storage**: `{azure_status}`\n"
        f"• **Default Cover Image**: `{cover_status}`\n\n"
        f"{summary}"
    )

    if status_msg:
        await status_msg.edit_text(report, parse_mode="Markdown")
    elif update.effective_message:
        await update.effective_message.reply_text(report, parse_mode="Markdown")


# =====================================================================
# Stage 2: YouTube URL Detection & Download Pipeline
# =====================================================================

async def _update_progress_message(
    status_msg: Message,
    text: str,
) -> None:
    """Safely edits the status message without crashing on Telegram API errors."""
    try:
        await status_msg.edit_text(text, parse_mode="Markdown")
    except Exception as exc:
        # Ignore Telegram 'Message is not modified' or minor rate-limit blips
        logger.debug("Minor exception editing progress message: %s", exc)


async def _run_download_background(
    job_id: str,
    url: str,
    status_msg: Message,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """
    Runs the download in background, manages rate-limited Telegram edits,
    and updates SQLite job state upon completion or failure.
    """
    logger.info("[%s] Starting background download task for url: %s", job_id, url)
    last_edit_time = 0.0
    MIN_EDIT_INTERVAL = 1.8  # Seconds between Telegram edits to avoid 429 errors

    def progress_callback(percent: float, speed_str: str, eta_str: str) -> None:
        nonlocal last_edit_time
        now = time.time()
        if now - last_edit_time >= MIN_EDIT_INTERVAL:
            last_edit_time = now
            msg_text = (
                f"📥 **Downloading YouTube Video**\n\n"
                f"• **Job ID**: `{job_id[:8]}...`\n"
                f"• **Progress**: `{percent:.1f}%`\n"
                f"• **Speed**: `{speed_str}`\n"
                f"• **ETA**: `{eta_str}`"
            )
            # Schedule message update on the main event loop thread-safely
            asyncio.run_coroutine_threadsafe(
                _update_progress_message(status_msg, msg_text),
                loop,
            )

    try:
        result = await downloader.download(
            job_id=job_id,
            url=url,
            progress_callback=progress_callback,
        )

        # Step 1 Success notification & transition to Step 2
        duration_str = _format_seconds(result.duration)
        size_str = _format_bytes(result.file_size)
        logger.info("[%s] Download succeeded: '%s' (%s). Starting Memoxz processing...", job_id, result.title, size_str)

        proc_start_msg = (
            f"⚙️ **Processing Video (Memoxz Anti-Fingerprint)**\n\n"
            f"• **Job ID**: `{job_id[:8]}...`\n"
            f"• **Title**: {result.title}\n"
            f"• **Status**: Initializing FFmpeg (CRF 17 Lossless | 320k AAC)..."
        )
        await _update_progress_message(status_msg, proc_start_msg)

        # Progress callback for FFmpeg
        last_proc_edit_time = 0.0
        def proc_progress_callback(pct: int, curr_sec: float, total_sec: float) -> None:
            nonlocal last_proc_edit_time
            now = time.time()
            if now - last_proc_edit_time >= MIN_EDIT_INTERVAL:
                last_proc_edit_time = now
                bar_len = 16
                filled = int(bar_len * pct / 100)
                bar = "█" * filled + "░" * (bar_len - filled)
                time_info = f"{curr_sec:.1f}s / {total_sec:.1f}s" if total_sec > 0 else f"{curr_sec:.1f}s"
                p_text = (
                    f"⚙️ **Processing Video (Memoxz Engine)**\n\n"
                    f"• **Job ID**: `{job_id[:8]}...`\n"
                    f"• **Progress**: `[{bar}] {pct}%`\n"
                    f"• **Render Time**: `{time_info}`\n"
                    f"• **Quality**: `CRF 17 Studio Master | 320k AAC`\n"
                    f"• **Metadata**: `Adobe Premiere Pro CC 2024`"
                )
                asyncio.run_coroutine_threadsafe(
                    _update_progress_message(status_msg, p_text),
                    loop,
                )

        proc_result = await video_processor.process_video(
            job_id=job_id,
            preset="balanced",
            progress_callback=proc_progress_callback,
        )

        final_duration_str = _format_seconds(proc_result.duration)
        final_size_str = _format_bytes(proc_result.file_size)
        final_msg = (
            f"🎬 **Processing Complete (Instagram Ready)**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Title**: {result.title}\n"
            f"• **Resolution**: `{proc_result.width}x{proc_result.height}`\n"
            f"• **Duration**: `{final_duration_str}`\n"
            f"• **Size**: `{final_size_str}`\n"
            f"• **Metadata**: `Adobe Premiere Pro CC 2024 Injected`\n"
            f"• **Cover Image**: Attached (`{config.default_cover_path.name}`)\n"
            f"• **Status**: Ready for Azure Blob Upload & Instagram Publishing (Stage 5)"
        )
        await _update_progress_message(status_msg, final_msg)
        logger.info("[%s] Memoxz video processing pipeline completed successfully.", job_id)

    except Exception as exc:
        err_msg = str(exc)
        clean_err = re.sub(r"^\[[A-Z_]+\]\s*", "", err_msg)
        logger.error("[%s] Background download/processing pipeline failed: %s", job_id, err_msg)

        failure_msg = (
            f"❌ **Task Failed**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Reason**: {clean_err}\n\n"
            f"Please check the URL or try again."
        )
        await _update_progress_message(status_msg, failure_msg)


@restricted
async def youtube_url_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Detects YouTube URLs in text messages from authorized users, registers
    a job in SQLite, acknowledges immediately, and begins background download.
    """
    message = update.effective_message
    if not message or not message.text:
        return

    match = YOUTUBE_URL_REGEX.search(message.text)
    if not match:
        # Message is not a YouTube URL; ignore or let other handlers process
        return

    url = match.group(1).strip()
    user = update.effective_user
    user_id = user.id if user else 0

    # 1. Generate unique Job ID (UUID4)
    job_id = str(uuid.uuid4())
    logger.info("New download job registered: job_id=%s, user_id=%s, url=%s", job_id, user_id, url)

    # 2. Persist in SQLite Job Store
    job_store.create_job(job_id=job_id, user_id=user_id, source_url=url)

    # 3. Send immediate acknowledgment message
    ack_text = (
        f"📥 **Download Request Received**\n\n"
        f"• **Job ID**: `{job_id}`\n"
        f"• **Status**: Connecting to YouTube..."
    )
    status_msg = await message.reply_text(ack_text, parse_mode="Markdown")

    # 4. Start background download task without blocking the polling event loop
    loop = asyncio.get_running_loop()
    asyncio.create_task(_run_download_background(job_id, url, status_msg, loop))


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global error handler for unhandled exceptions in any handler.
    Logs the error with traceback and ensures the bot continues running.
    """
    logger.error(
        "Unhandled exception while processing Telegram update: %s",
        context.error,
        exc_info=context.error,
    )


# =====================================================================
# Stubs for Subsequent Stages
# =====================================================================

async def download_youtube_video(url: str, job_id: Optional[str] = None) -> DownloadResult:
    """
    Public entry point for downloading YouTube videos.
    Uses YouTubeDownloader implemented in Stage 2.
    """
    jid = job_id or str(uuid.uuid4())
    return await downloader.download(job_id=jid, url=url)


async def process_video(
    input_path: str,
    job_id: str,
    preset: str = "balanced",
    instructions: Optional[dict] = None,
) -> ProcessedResult:
    """
    Public entry point for video processing.
    Uses Memoxz VideoProcessor implemented in Stage 3.
    """
    return await video_processor.process_video(
        job_id=job_id,
        preset=preset,
        instructions=instructions,
    )


async def publish_to_instagram(video_url: str, caption: str, job_id: str) -> str:
    """
    Placeholder for uploading to Azure Blob and publishing to Instagram.
    # TODO: Stage 5 - Implement Azure Blob upload and temporary SAS URL generation
    # TODO: Stage 6 - Implement Instagram Graph API publishing with container creation and polling
    """
    raise NotImplementedError("Stage 5/6 not implemented yet.")
