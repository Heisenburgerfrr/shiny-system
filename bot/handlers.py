"""Telegram command handlers, access control, diagnostic checks, and video download handlers."""

import asyncio
import functools
import logging
import re
import time
import uuid
from typing import Callable, Dict, Optional, Tuple

import httpx
from azure.storage.blob import BlobServiceClient
from telegram import Message, Update
from telegram.ext import ContextTypes

from bot.azure_storage import azure_storage_manager
from bot.config import config
from bot.cover import inspect_cover_image
from bot.db import job_store
from bot.downloader import DownloadResult, _format_bytes, _format_seconds, downloader
from bot.instagram_publish import (
    InstagramPublishError,
    InstagramRateLimitError,
    InstagramTokenExpiredError,
    instagram_publisher,
)
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
    Verifies Instagram Graph API connectivity and returns business account username.
    Uses long-lived access token configured in INSTAGRAM_ACCESS_TOKEN.
    """
    url = f"https://graph.facebook.com/v21.0/{config.instagram_business_account_id}"
    params = {
        "fields": "id,username",
        "access_token": config.instagram_access_token,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
            if resp.status_code == 200 and "username" in data:
                return True, f"Connected (@{data['username']})"
            error_detail = data.get("error", {}).get("message", resp.text)
            logger.error("Instagram Graph API check failed: HTTP %s: %s", resp.status_code, error_detail)
            return False, f"Failed (HTTP {resp.status_code}: {error_detail[:80]})"
    except Exception as exc:
        logger.error("Instagram Graph API check failed with exception: %s", exc)
        return False, f"Error ({type(exc).__name__}: {str(exc)[:80]})"


def _check_azure_sync() -> Tuple[bool, str]:
    """
    Synchronously verifies Azure Blob Storage reachability and container existence.
    """
    try:
        blob_service_client = BlobServiceClient.from_connection_string(
            config.azure_storage_connection_string,
            connection_timeout=5,
            read_timeout=5,
            retry_total=0,
        )
        account_info = blob_service_client.get_account_information(timeout=5)
        sku_name = account_info.get("sku_name", "standard")
        container_client = blob_service_client.get_container_client(config.azure_blob_container)
        exists = container_client.exists(timeout=5)
        if exists:
            return True, f"Reachable (Account SKU: {sku_name}, Container '{config.azure_blob_container}': OK)"
        else:
            return False, f"Reachable (Account SKU: {sku_name}, Container '{config.azure_blob_container}': Not Found)"
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
        logger.info("[%s] Memoxz video processing pipeline completed successfully.", job_id)

        # =====================================================================
        # Stage 5: Upload to Azure Blob Storage & Generate Public SAS URLs
        # =====================================================================
        job_store.start_azure_upload(job_id)
        upload_msg = (
            f"☁️ **Uploading to Azure Blob Storage...**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Container**: `{config.azure_blob_container}`\n"
            f"• **Video Size**: `{final_size_str}`\n"
            f"• Generating secure SAS access for Instagram ingestion..."
        )
        await _update_progress_message(status_msg, upload_msg)

        try:
            # 1. Ensure cover image is hosted
            cover_data = await azure_storage_manager.ensure_cover_uploaded(config.default_cover_path)

            # 2. Upload video blob with retries and verify reachability
            video_data = await azure_storage_manager.upload_video_blob(
                job_id=job_id,
                file_path=proc_result.output_path,
            )

            # 3. Transition job status to 'hosted' in SQLite
            job_store.complete_azure_upload(
                job_id=job_id,
                blob_name=video_data["blob_name"],
                sas_url=video_data["sas_url"],
                expires_at=video_data["expires_at"],
            )

            expiry_display = video_data["expires_at"][:19].replace("T", " ") + " UTC"
            logger.info("[%s] Azure public hosting pipeline completed successfully.", job_id)

            # =====================================================================
            # Stage 6: Instagram Publishing Workflow (Inline or Guided)
            # =====================================================================
            job = job_store.get_job(job_id)
            existing_caption = job.get("caption") if job else None

            if existing_caption:
                # User provided inline caption alongside the YouTube link!
                logger.info(
                    "[%s] Pre-supplied caption detected (%d chars). Auto-publishing to Instagram...",
                    job_id,
                    len(existing_caption),
                )
                await _update_progress_message(
                    status_msg,
                    f"☁️ **Hosting Complete (Azure)**\n\n"
                    f"• **Job ID**: `{job_id}`\n"
                    f"• **Caption**: {existing_caption[:80]}...\n"
                    f"• 🚀 Auto-launching Instagram Reels publishing..."
                )
                await _run_instagram_publish_background(job_id, existing_caption, status_msg)
            else:
                # Bare link without caption -> prompt user in Telegram
                job_store.set_awaiting_caption(job_id)
                prompt_msg = (
                    f"🎬 **Video Ready for Instagram!**\n\n"
                    f"• **Job ID**: `{job_id}`\n"
                    f"• **Title**: {result.title}\n"
                    f"• **Video**: `{video_data['blob_name']}` (Verified Reachable)\n\n"
                    f"💬 **Please reply with the caption** for this Instagram Reel.\n"
                    f"*(Or reply `/skip` to use the YouTube video title)*"
                )
                await _update_progress_message(status_msg, prompt_msg)

        except Exception as upload_exc:
            err_text = str(upload_exc)
            clean_upload_err = re.sub(r"^\[[A-Z_]+\]\s*", "", err_text)
            job_store.fail_azure_upload(job_id, err_text)
            logger.error("[%s] Azure Blob upload pipeline failed: %s", job_id, err_text, exc_info=True)

            user_friendly_fail = (
                f"❌ **Azure Hosting Failed**\n\n"
                f"• **Job ID**: `{job_id}`\n"
                f"• **Reason**: Unable to host video on Azure Blob Storage ({clean_upload_err}).\n\n"
                f"Please verify your Azure storage credentials and network connectivity, or try again."
            )
            await _update_progress_message(status_msg, user_friendly_fail)
            return

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


async def _run_instagram_publish_background(
    job_id: str,
    caption: str,
    status_msg: Message,
) -> None:
    """Executes the 3-step Instagram Reels publishing pipeline with throttled progress updates."""
    job_store.start_publishing(job_id, caption=caption)
    job = job_store.get_job(job_id)
    if not job:
        logger.error("[%s] Job not found for publishing.", job_id)
        return

    video_sas_url = job.get("video_sas_url")
    if not video_sas_url:
        logger.error("[%s] No video SAS URL found on job record.", job_id)
        job_store.fail_publishing(job_id, "Missing video SAS URL")
        await _update_progress_message(
            status_msg,
            f"❌ **Publish Failed**: Missing hosted video URL for job `{job_id}`.",
        )
        return

    cover_data = await azure_storage_manager.ensure_cover_uploaded(config.default_cover_path)
    cover_sas_url = cover_data.get("sas_url")

    last_update_time = 0.0

    async def publish_progress(status_code: str, elapsed: float) -> None:
        nonlocal last_update_time
        now = time.monotonic()
        if now - last_update_time >= 3.0:
            last_update_time = now
            msg_text = (
                f"🚀 **Publishing Reel to Instagram...**\n\n"
                f"• **Job ID**: `{job_id}`\n"
                f"• **Status**: Container `{status_code}` ({int(elapsed)}s elapsed)\n"
                f"• Meta is ingesting and rendering Reel..."
            )
            await _update_progress_message(status_msg, msg_text)

    try:
        publish_res = await instagram_publisher.publish_reel(
            job_id=job_id,
            video_url=video_sas_url,
            caption=caption,
            cover_url=cover_sas_url,
            progress_callback=publish_progress,
        )

        success_msg = (
            f"🎉 **Reel Published Successfully!**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Media ID**: `{publish_res['media_id']}`\n"
            f"• **Post Link**: {publish_res['permalink']}\n"
            f"• **Azure Cleanup**: Video blob removed.\n\n"
            f"✨ [View Live Post on Instagram]({publish_res['permalink']})"
        )
        await _update_progress_message(status_msg, success_msg)
        logger.info("[%s] Instagram Reel successfully published: %s", job_id, publish_res["permalink"])

    except InstagramTokenExpiredError as token_err:
        job_store.fail_publishing(job_id, str(token_err))
        logger.critical("[%s] CRITICAL: Instagram access token has expired: %s", job_id, token_err)
        alert_msg = (
            f"🚨 **CRITICAL: Instagram Access Token Expired!**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Error**: Meta rejected the access token as expired or invalid.\n\n"
            f"⚠️ **Action Required**: Please generate a new 60-day long-lived access token and update `INSTAGRAM_ACCESS_TOKEN` in your environment."
        )
        await _update_progress_message(status_msg, alert_msg)

    except InstagramRateLimitError as rate_err:
        job_store.fail_publishing(job_id, str(rate_err))
        logger.warning("[%s] Instagram rate limit reached: %s", job_id, rate_err)
        rate_msg = (
            f"⏳ **Instagram Rate Limit Reached**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Reason**: {rate_err}\n\n"
            f"Please wait before publishing more Reels."
        )
        await _update_progress_message(status_msg, rate_msg)

    except Exception as exc:
        err_msg = str(exc)
        job_store.fail_publishing(job_id, err_msg)
        logger.error("[%s] Instagram publishing failed: %s", job_id, err_msg, exc_info=True)
        fail_msg = (
            f"❌ **Instagram Publish Failed**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Reason**: {err_msg}\n\n"
            f"Please review logs or try again."
        )
        await _update_progress_message(status_msg, fail_msg)


@restricted
async def youtube_url_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Detects YouTube URLs in text messages from authorized users, registers
    a job in SQLite, acknowledges immediately, and begins background download.
    Also handles replies/captions for jobs waiting in 'awaiting_caption' state.
    """
    message = update.effective_message
    if not message or not message.text:
        return

    user = update.effective_user
    user_id = user.id if user else 0

    match = YOUTUBE_URL_REGEX.search(message.text)
    if not match:
        # Check if the user is replying with a caption for an active job
        awaiting_job = job_store.get_active_awaiting_caption_job(user_id)
        if awaiting_job:
            caption_text = message.text.strip()
            # If user sent /skip, default to the original YouTube video title
            if caption_text.lower() == "/skip":
                caption_text = awaiting_job.get("title") or "New Reel"

            job_id = awaiting_job["job_id"]
            status_msg = await message.reply_text(
                f"📝 Caption saved for job `{job_id}`:\n\n"
                f"_{caption_text[:120]}..._\n\n"
                f"🚀 Launching Instagram publishing...",
                parse_mode="Markdown",
            )
            asyncio.create_task(_run_instagram_publish_background(job_id, caption_text, status_msg))
            return
        return

    url = match.group(1).strip()

    # Extract inline caption (everything other than the URL)
    # Allows sending: https://youtube.com/watch?v=xyz My multiline caption here
    raw_text = message.text
    caption_part = raw_text.replace(match.group(0), "").strip()
    inline_caption = caption_part if caption_part else None

    # 1. Generate unique Job ID (UUID4)
    job_id = str(uuid.uuid4())
    logger.info(
        "New download job registered: job_id=%s, user_id=%s, url=%s, has_inline_caption=%s",
        job_id,
        user_id,
        url,
        bool(inline_caption),
    )

    # 2. Persist in SQLite Job Store (with caption if provided)
    job_store.create_job(job_id=job_id, user_id=user_id, source_url=url, caption=inline_caption)

    # 3. Send immediate acknowledgment message
    caption_note = " (with custom caption)" if inline_caption else ""
    ack_text = (
        f"📥 **Download Request Received{caption_note}**\n\n"
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
# Public Pipeline Helpers
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


async def publish_to_instagram(video_url: str, caption: str, job_id: str) -> Dict[str, str]:
    """
    Public entry point for publishing hosted video to Instagram Reels.
    Uses InstagramPublisher implemented in Stage 6.
    """
    cover_data = await azure_storage_manager.ensure_cover_uploaded(config.default_cover_path)
    return await instagram_publisher.publish_reel(
        job_id=job_id,
        video_url=video_url,
        caption=caption,
        cover_url=cover_data.get("sas_url"),
    )

