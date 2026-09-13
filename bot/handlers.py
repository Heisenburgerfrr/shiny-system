"""Telegram command handlers, access control, diagnostic checks, and video download handlers."""

import asyncio
from datetime import datetime, timezone
import functools
import logging
from pathlib import Path
import re
import shutil
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

import httpx
from azure.storage.blob import BlobServiceClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from bot.azure_storage import azure_storage_manager
from bot.config import BASE_DIR, config
from bot.cover import crop_and_save_cover_image, inspect_cover_image
from bot.db import job_store
from bot.downloader import DownloadResult, _format_bytes, _format_seconds, downloader
from bot.instagram_publish import (
    InstagramPublishError,
    InstagramRateLimitError,
    InstagramTokenExpiredError,
    instagram_publisher,
)
from bot.processor import PROCESSED_DIR, ProcessedResult, video_processor
from bot.recovery import (
    failure_tracker,
    startup_recovery,
    storage_cleaner,
    task_registry,
)
from bot.render_queue import render_queue

logger = logging.getLogger("bot.handlers")

# Regex to detect YouTube URLs (videos, shorts, youtu.be, live streams)
YOUTUBE_URL_REGEX = re.compile(
    r"(https?://(?:www\.)?(?:youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/|embed/)|youtu\.be/)[a-zA-Z0-9_\-]+[^\s]*)",
    re.IGNORECASE,
)

# Regex to detect Instagram URLs (reels, p/posts, share links)
INSTAGRAM_URL_REGEX = re.compile(
    r"(https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:reel|reels|p|share)/[a-zA-Z0-9_\-]+[^\s]*)",
    re.IGNORECASE,
)

# Unified Media URL Regex matching either YouTube or Instagram
MEDIA_URL_REGEX = re.compile(
    r"(https?://(?:www\.)?(?:youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/|embed/)|youtu\.be/|(?:instagram\.com|instagr\.am)/(?:reel|reels|p|share)/)[a-zA-Z0-9_\-]+[^\s]*)",
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
        "👋 **Welcome to Media Publisher!**\n\n"
        "Send any **YouTube link** (Shorts/Videos) or **Instagram link** (Reels or Carousel sliding posts) to automatically optimize and publish to Instagram!\n\n"
        "🎬 **Supported Formats:**\n"
        "• **YouTube Shorts / Videos** ➔ Instagram Reels\n"
        "• **Instagram Reels** (`/reel/`) ➔ Instagram Reels\n"
        "• **Instagram Carousels** (`/p/`) ➔ 1–10 slides (videos, images, or mixed) as Instagram Carousel posts\n\n"
        "⚙️ **Smart Render Queue:**\n"
        "• Heavy FFmpeg video rendering is safely queued (1 at a time) to prevent VM CPU/memory overload while downloads run concurrently.\n\n"
        "💡 **How it works:**\n"
        "• Send **only a link** and the bot uses your default caption template.\n"
        "• Or send a link with a **custom caption** in the same message to use that caption.\n\n"
        "⚡ **Commands:**\n"
        "• `/caption` — View or edit default caption template\n"
        "• `/cover` — View or update Reels cover image\n"
        "• `/cookies` — View or update YouTube cookies.txt\n"
        "• `/status` — Check API & service health\n"
        "• `/jobs` — View active processing tasks\n"
        "• `/cancel <id>` — Cancel a task\n"
        "• `/retry <id>` — Retry a failed task"
    )
    if update.effective_message:
        await update.effective_message.reply_text(welcome_message, parse_mode="Markdown")


DEFAULT_MEME_CAPTION = (
    "TVT=× \n\n"
    "TONE PIECE. \n\n"
    "エルバフ編 最新情報を発表&最新PV公開 \n\n"
    "オープニング主題歌&エンディング主題歌、さら \n\n"
    "にエルバフ編の重要キャラクター \n\n"
    "「ロキ」のキャストが決定しました"
)


@restricted
async def caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles /caption: displays the current default caption template,
    or immediately updates it if arguments were provided (/caption <new text>).
    """
    message = update.effective_message
    if not message:
        return

    # Check if arguments were passed directly: /caption <new text>
    if context.args:
        cmd_parts = message.text.split(maxsplit=1)
        new_caption = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""
        if new_caption:
            job_store.set_setting("default_caption", new_caption)
            context.user_data["awaiting"] = None
            await message.reply_text(
                f"✅ **Default caption updated!**\n\n"
                f"📝 **New Template:**\n{new_caption}\n\n"
                f"💡 All YouTube links sent without a custom caption will use this automatically.",
                parse_mode="Markdown",
            )
            return

    current = job_store.get_setting("default_caption") or DEFAULT_MEME_CAPTION

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Change Default Caption", callback_data="change_caption")]
    ])

    await message.reply_text(
        f"📝 **Current Default Caption Template:**\n\n"
        f"{current}\n\n"
        f"💡 *YouTube links sent without a caption will automatically use this template.*",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


@restricted
async def cover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles /cover: displays the current Reels cover image with resolution and specs,
    and provides an inline button to change it.
    """
    message = update.effective_message
    if not message:
        return

    cover_path = config.default_cover_path
    cover_info = inspect_cover_image(cover_path)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Change Cover Image", callback_data="change_cover")]
    ])

    if cover_path.exists() and cover_info.is_valid:
        caption_text = (
            f"🖼️ **Current Reels Cover Image**\n\n"
            f"• **Dimensions**: `{cover_info.width}x{cover_info.height}` ({cover_info.format})\n"
            f"• **Aspect Ratio**: 9:16 (Instagram Reels compliant)\n"
            f"• **File Size**: `{cover_info.file_size / 1024:.1f} KB`\n\n"
            f"💡 *Click below to upload a new cover image. It will be automatically cropped to 9:16.*"
        )
        with open(cover_path, "rb") as photo_f:
            await message.reply_photo(
                photo=photo_f,
                caption=caption_text,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
    else:
        await message.reply_text(
            f"🖼️ **Reels Cover Image**\n\n"
            f"Status: `{cover_info.status_summary}` ({cover_info.details})\n\n"
            f"Click below to upload a new cover image.",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )


@restricted
async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles /cookies: displays current cookies.txt status (file size, presence of login tokens)
    or immediately updates cookies if text was passed (/cookies <pasted lines>).
    Provides action buttons to either upload a .txt file or paste raw text.
    """
    message = update.effective_message
    if not message:
        return

    # Check if arguments were passed directly: /cookies <pasted lines>
    if message.text:
        cmd_parts = message.text.split(None, 1)
        if len(cmd_parts) > 1 and cmd_parts[1].strip():
            raw_text = cmd_parts[1].strip()
            target_path = BASE_DIR / "cookies.txt"
            target_path.write_text(raw_text, encoding="utf-8")
            has_auth = any(token in raw_text for token in ["LOGIN_INFO", "__Secure-3PSID", "SAPISID", "SID"])
            size_kb = len(raw_text.encode("utf-8")) / 1024.0
            auth_badge = "✅ Authenticated (Active login session)" if has_auth else "⚠️ Warning: No login tokens detected (Guest session)"
            await message.reply_text(
                f"🍪 **YouTube Cookies Updated Successfully!**\n\n"
                f"• **Method**: Direct argument saved to `{target_path.name}`\n"
                f"• **File Size**: `{size_kb:.1f} KB`\n"
                f"• **Session**: {auth_badge}\n\n"
                f"✨ All future YouTube downloads will use these cookies immediately.",
                parse_mode="Markdown",
            )
            return

    cookies_path = BASE_DIR / "cookies.txt"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 Upload .txt File", callback_data="change_cookies_file"),
            InlineKeyboardButton("📋 Paste Cookie Text", callback_data="change_cookies_paste"),
        ]
    ])

    if cookies_path.is_file():
        size_kb = cookies_path.stat().st_size / 1024.0
        has_auth = False
        try:
            content = cookies_path.read_text(encoding="utf-8", errors="ignore")
            has_auth = any(token in content for token in ["LOGIN_INFO", "__Secure-3PSID", "SAPISID", "SID"])
        except Exception:
            pass

        auth_status = "✅ Authenticated (Active login session)" if has_auth else "⚠️ Guest / Logged-out session"
        msg = (
            f"🍪 **Current YouTube Cookies Status**\n\n"
            f"• **Location**: `{cookies_path.name}`\n"
            f"• **File Size**: `{size_kb:.1f} KB`\n"
            f"• **Auth Status**: {auth_status}\n\n"
            f"💡 *Choose an option below, or simply drop `cookies.txt` or paste your cookie text directly into this chat.*"
        )
    else:
        msg = (
            f"🍪 **YouTube Cookies Status**\n\n"
            f"⚠️ `cookies.txt` is not currently present on the server.\n\n"
            f"Send your exported `cookies.txt` file or paste the cookie text directly into this chat."
        )

    await message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")


@restricted
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline button clicks for changing caption or cover image."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if query.data == "change_caption":
        context.user_data["awaiting"] = "caption"
        await query.message.reply_text(
            "✍️ **Send your new default caption now.**\n\n"
            "Simply send your desired caption as a text message, and it will be saved as the default template for all future Reels.",
            parse_mode="Markdown",
        )
    elif query.data == "change_cover":
        context.user_data["awaiting"] = "cover"
        await query.message.reply_text(
            "📸 **Send your new cover image now.**\n\n"
            "Send any photo or image. It will be automatically center-cropped to 9:16 and resized to 1080x1920 for Instagram Reels.",
            parse_mode="Markdown",
        )
    elif query.data in ("change_cookies", "change_cookies_file"):
        context.user_data["awaiting"] = "cookies"
        await query.message.reply_text(
            "📤 **Send your `cookies.txt` file now.**\n\n"
            "Attach and send your exported `cookies.txt` file into this chat.",
            parse_mode="Markdown",
        )
    elif query.data == "change_cookies_paste":
        context.user_data["awaiting"] = "cookies"
        await query.message.reply_text(
            "📋 **Paste your cookies text now.**\n\n"
            "Paste the Netscape-format cookie lines directly as a message into this chat.",
            parse_mode="Markdown",
        )


@restricted
async def photo_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles uploaded photos or images from authorized users.
    Auto-crops to 9:16, resizes to 1080x1920, and sets as the active default cover image.
    """
    import tempfile
    message = update.effective_message
    if not message:
        return

    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id

    if not file_id:
        return

    status_msg = await message.reply_text("⏳ Processing and optimizing cover image for Reels...")

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=tmp_path)

        target_path = config.default_cover_path
        res = crop_and_save_cover_image(tmp_path, target_path)

        azure_storage_manager._cover_cache.clear()
        context.user_data["awaiting"] = None

        success_text = (
            f"✅ **New Reels Cover Applied!**\n\n"
            f"• **Resolution**: `{res.width}x{res.height}` (9:16)\n"
            f"• **Format**: `{res.format}` ({res.file_size / 1024:.1f} KB)\n"
            f"• **Optimization**: Center-cropped to 9:16 aspect ratio\n\n"
            f"✨ All future Instagram Reels will automatically use this cover!"
        )

        with open(target_path, "rb") as photo_f:
            await message.reply_photo(
                photo=photo_f,
                caption=success_text,
                parse_mode="Markdown",
            )
        await status_msg.delete()

    except Exception as exc:
        logger.error("Failed to process uploaded cover image: %s", exc, exc_info=True)
        await status_msg.edit_text(f"❌ Failed to process cover image: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)


@restricted
async def document_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles uploaded documents from authorized users.
    Specifically validates and installs new cookies.txt files.
    """
    import tempfile
    import shutil
    message = update.effective_message
    if not message or not message.document:
        return

    doc = message.document
    filename = (doc.file_name or "").lower()

    user_data = getattr(context, "user_data", None)
    awaiting_cookies = isinstance(user_data, dict) and user_data.get("awaiting") == "cookies"

    # Process if named cookies.txt, ends with .txt, or user clicked [Upload New cookies.txt]
    if not (filename == "cookies.txt" or filename.endswith(".txt") or awaiting_cookies):
        return

    status_msg = await message.reply_text("⏳ Verifying and installing `cookies.txt`...")

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=tmp_path)

        content = tmp_path.read_text(encoding="utf-8", errors="ignore")

        # Validation: check for Netscape cookie signatures or domain names
        is_netscape = ("# Netscape" in content or "# HTTP Cookie File" in content)
        is_youtube = ("youtube.com" in content or "google.com" in content)

        if not (is_netscape or is_youtube or "\t" in content):
            await status_msg.edit_text(
                "❌ **Invalid Cookies File**\n\n"
                "The file does not appear to be a Netscape-format `cookies.txt` exported from YouTube.\n"
                "Please export your cookies using the *Get cookies.txt LOCALLY* extension while on youtube.com."
            )
            return

        target_path = BASE_DIR / "cookies.txt"
        shutil.copy2(tmp_path, target_path)

        # Check for authentication tokens
        has_auth = any(token in content for token in ["LOGIN_INFO", "__Secure-3PSID", "SAPISID", "SID"])
        size_kb = target_path.stat().st_size / 1024.0

        if isinstance(user_data, dict):
            user_data["awaiting"] = None

        auth_badge = "✅ Authenticated (Active login session)" if has_auth else "⚠️ Warning: No login tokens detected (Guest session)"
        success_msg = (
            f"🍪 **YouTube Cookies Updated Successfully!**\n\n"
            f"• **Destination**: `{target_path.resolve()}`\n"
            f"• **File Size**: `{size_kb:.1f} KB`\n"
            f"• **Session**: {auth_badge}\n\n"
            f"✨ All future YouTube downloads will use these cookies immediately."
        )
        await status_msg.edit_text(success_msg, parse_mode="Markdown")
        logger.info("Successfully updated cookies.txt via Telegram (size: %.1f KB, authenticated=%s)", size_kb, has_auth)

    except Exception as exc:
        logger.error("Failed to process uploaded cookies.txt: %s", exc, exc_info=True)
        await status_msg.edit_text(f"❌ Failed to install cookies: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)


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
    tg_status = "Connected"

    # 2. Instagram Graph API check
    ig_ok, ig_detail = await _check_instagram()
    ig_status = ig_detail if ig_ok else f"Error: {ig_detail}"

    # 3. Azure Blob Storage check
    azure_ok, azure_detail = await _check_azure()
    azure_status = "Connected" if azure_ok else f"Error: {azure_detail}"

    # 4. Default Cover Image check (Stage 4)
    cover_res = inspect_cover_image(config.default_cover_path)
    cover_status = "Ready" if cover_res.is_valid else f"Warning: {cover_res.details}"

    overall_ok = ig_ok and azure_ok and cover_res.is_valid
    summary = "✅ All systems operational." if overall_ok else "⚠️ One or more checks failed. Review logs."

    report = (
        f"📊 **System Status**\n\n"
        f"• **Telegram Bot**: `{tg_status}`\n"
        f"• **Instagram API**: `{ig_status}`\n"
        f"• **Azure Storage**: `{azure_status}`\n"
        f"• **Cover Image**: `{cover_status}`\n\n"
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
    current_task = asyncio.current_task()
    if current_task:
        task_registry.register_task(job_id, current_task)

    logger.info("[%s] Starting background download task for url: %s", job_id, url)
    last_edit_time = 0.0
    MIN_EDIT_INTERVAL = 1.8  # Seconds between Telegram edits to avoid 429 errors

    def progress_callback(percent: float, speed_str: str, eta_str: str) -> None:
        nonlocal last_edit_time
        now = time.time()
        if now - last_edit_time >= MIN_EDIT_INTERVAL:
            last_edit_time = now
            bar_len = 10
            filled = int(bar_len * percent / 100)
            bar = "█" * filled + "░" * (bar_len - filled)
            msg_text = (
                f"⚡ **Processing Reel...**\n\n"
                f"📥 **Downloading**: `[{bar}] {percent:.0f}%` ({speed_str})"
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

        failure_tracker.record_success("download")

        # Step 1 Success notification & transition to Step 2
        duration_str = _format_seconds(result.duration)
        size_str = _format_bytes(result.file_size)
        logger.info("[%s] Download succeeded: '%s' (%s). Starting processing...", job_id, result.title, size_str)

        # Branch for multi-slide carousel posts
        if result.is_carousel and result.carousel_items and len(result.carousel_items) > 1:
            await _handle_carousel_pipeline(
                job_id=job_id,
                result=result,
                status_msg=status_msg,
                loop=loop,
            )
            return

        # Single video flow (YouTube Shorts, YouTube Video, or single Instagram Reel)
        async def on_queue_wait(position: int) -> None:
            wait_text = (
                f"⏳ **Waiting in Render Queue...**\n\n"
                f"🎬 **{result.title}**\n\n"
                f"Position: `#{position}` in queue (Rendering 1 at a time to prevent server overload)"
            )
            await _update_progress_message(status_msg, wait_text)

        proc_start_msg = (
            f"⚡ **Processing Video...**\n\n"
            f"🎬 **{result.title}**\n\n"
            f"⚙️ Optimizing video format..."
        )
        await _update_progress_message(status_msg, proc_start_msg)

        # Progress callback for FFmpeg
        last_proc_edit_time = 0.0
        def proc_progress_callback(pct: int, curr_sec: float, total_sec: float) -> None:
            nonlocal last_proc_edit_time
            now = time.time()
            if now - last_proc_edit_time >= MIN_EDIT_INTERVAL:
                last_proc_edit_time = now
                bar_len = 10
                filled = int(bar_len * pct / 100)
                bar = "█" * filled + "░" * (bar_len - filled)
                p_text = (
                    f"⚡ **Processing Video...**\n\n"
                    f"🎬 **{result.title}**\n\n"
                    f"✂️ **Rendering**: `[{bar}] {pct}%`"
                )
                asyncio.run_coroutine_threadsafe(
                    _update_progress_message(status_msg, p_text),
                    loop,
                )

        async with render_queue.acquire_slot(job_id, on_wait_callback=on_queue_wait):
            proc_result = await video_processor.process_video(
                job_id=job_id,
                preset="balanced",
                input_file=Path(result.file_path) if result.file_path else None,
                progress_callback=proc_progress_callback,
            )

        failure_tracker.record_success("processing")
        final_duration_str = _format_seconds(proc_result.duration)
        final_size_str = _format_bytes(proc_result.file_size)
        logger.info("[%s] Video processing completed successfully.", job_id)

        # =====================================================================
        # Stage 5: Upload to Azure Blob Storage & Generate Public SAS URLs
        # =====================================================================
        job_store.start_azure_upload(job_id)
        upload_msg = (
            f"⚡ **Processing Reel...**\n\n"
            f"🎬 **{result.title}**\n\n"
            f"☁️ Uploading to cloud storage..."
        )
        await _update_progress_message(status_msg, upload_msg)

        try:
            # 1. Ensure cover image is hosted
            cover_data = await azure_storage_manager.ensure_cover_uploaded(config.default_cover_path)

            # 2. Upload video blob with retries and verify reachability
            video_data = await azure_storage_manager.upload_video_blob(
                job_id=job_id,
                file_path=proc_result.file_path,
            )

            failure_tracker.record_success("azure_upload")

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
                    f"⚡ **Publishing Reel...**\n\n"
                    f"🎬 **{result.title}**\n\n"
                    f"🚀 Publishing to Instagram...",
                )
                asyncio.create_task(_run_instagram_publish_background(job_id, existing_caption, status_msg))
            else:
                # Bare link without caption -> prompt user in Telegram
                job_store.set_awaiting_caption(job_id)
                prompt_msg = (
                    f"🎬 **Video Ready!**\n\n"
                    f"**{result.title}**\n\n"
                    f"💬 Send the caption for this Reel, or `/skip` to publish directly."
                )
                await _update_progress_message(status_msg, prompt_msg)

        except asyncio.CancelledError:
            logger.info("[%s] Azure upload cancelled for job.", job_id)
            storage_cleaner.cleanup_job_local_files(job_id)
            raise
        except Exception as upload_exc:
            err_text = str(upload_exc)
            clean_upload_err = re.sub(r"^\[[A-Z_]+\]\s*", "", err_text)
            job_store.fail_azure_upload(job_id, err_text)
            logger.error("[%s] Azure Blob upload pipeline failed: %s", job_id, err_text, exc_info=True)
            sys_alert = failure_tracker.record_failure("azure_upload", job_id, err_text)
            storage_cleaner.cleanup_job_local_files(job_id)

            user_friendly_fail = (
                f"❌ **Azure Hosting Failed**\n\n"
                f"• **Job ID**: `{job_id}`\n"
                f"• **Reason**: Unable to host video on Azure Blob Storage ({clean_upload_err}).\n\n"
                f"Please verify your Azure storage credentials and network connectivity, or try `/retry {job_id}`."
            )
            if sys_alert:
                user_friendly_fail += f"\n\n{sys_alert}"
            await _update_progress_message(status_msg, user_friendly_fail)
            return

    except asyncio.CancelledError:
        logger.info("[%s] Background download/processing task was cancelled.", job_id)
        storage_cleaner.cleanup_job_local_files(job_id)
        raise
    except Exception as exc:
        err_msg = str(exc)
        clean_err = re.sub(r"^\[[A-Z_]+\]\s*", "", err_msg)
        stage = "processing" if ("processing" in err_msg.lower() or "ffmpeg" in err_msg.lower()) else "download"
        logger.error("[%s] Background %s pipeline failed: %s", job_id, stage, err_msg)
        sys_alert = failure_tracker.record_failure(stage, job_id, err_msg)
        storage_cleaner.cleanup_job_local_files(job_id)

        failure_msg = (
            f"❌ **Task Failed**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Reason**: {clean_err}\n\n"
            f"Use `/retry {job_id}` to retry or check the URL."
        )
        if sys_alert:
            failure_msg += f"\n\n{sys_alert}"
        await _update_progress_message(status_msg, failure_msg)
    finally:
        task_registry.unregister_task(job_id)


async def _run_instagram_publish_background(
    job_id: str,
    caption: str,
    status_msg: Message,
) -> None:
    """Executes the 3-step Instagram Reels publishing pipeline with throttled progress updates."""
    current_task = asyncio.current_task()
    if current_task:
        task_registry.register_task(job_id, current_task)

    job_store.start_publishing(job_id, caption=caption)
    job = job_store.get_job(job_id)
    if not job:
        logger.error("[%s] Job not found for publishing.", job_id)
        task_registry.unregister_task(job_id)
        return

    video_sas_url = job.get("video_sas_url")
    if not video_sas_url:
        logger.error("[%s] No video SAS URL found on job record.", job_id)
        job_store.fail_publishing(job_id, "Missing video SAS URL")
        task_registry.unregister_task(job_id)
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
            title = job.get("title") or "Instagram Reel"
            msg_text = (
                f"⚡ **Publishing Reel...**\n\n"
                f"🎬 **{title}**\n\n"
                f"🚀 Rendering on Instagram ({int(elapsed)}s)..."
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

        # Success - purge local files immediately and reset failure counters
        storage_cleaner.cleanup_job_local_files(job_id)
        failure_tracker.record_success("instagram_publish")

        title = job.get("title") or "Instagram Reel"
        success_msg = (
            f"🎉 **Reel Published!**\n\n"
            f"🎬 **{title}**\n\n"
            f"👉 [Watch on Instagram]({publish_res['permalink']})"
        )
        await _update_progress_message(status_msg, success_msg)
        logger.info("[%s] Instagram Reel successfully published: %s", job_id, publish_res["permalink"])

    except asyncio.CancelledError:
        logger.info("[%s] Instagram publish task was cancelled.", job_id)
        storage_cleaner.cleanup_job_local_files(job_id)
        raise

    except InstagramTokenExpiredError as token_err:
        job_store.fail_publishing(job_id, str(token_err))
        storage_cleaner.cleanup_job_local_files(job_id)
        sys_alert = failure_tracker.record_failure("token", job_id, str(token_err))
        logger.critical("[%s] CRITICAL: Instagram access token has expired: %s", job_id, token_err)
        alert_msg = (
            f"🚨 **CRITICAL: Instagram Access Token Expired!**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Error**: Meta rejected the access token as expired or invalid.\n\n"
            f"⚠️ **Action Required**: Please generate a new 60-day long-lived access token and update `INSTAGRAM_ACCESS_TOKEN` in your environment."
        )
        if sys_alert:
            alert_msg += f"\n\n{sys_alert}"
        await _update_progress_message(status_msg, alert_msg)

    except InstagramRateLimitError as rate_err:
        job_store.fail_publishing(job_id, str(rate_err))
        storage_cleaner.cleanup_job_local_files(job_id)
        sys_alert = failure_tracker.record_failure("instagram_publish", job_id, str(rate_err))
        logger.warning("[%s] Instagram rate limit reached: %s", job_id, rate_err)
        rate_msg = (
            f"⏳ **Instagram Rate Limit Reached**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Reason**: {rate_err}\n\n"
            f"Please wait before publishing more Reels."
        )
        if sys_alert:
            rate_msg += f"\n\n{sys_alert}"
        await _update_progress_message(status_msg, rate_msg)

    except Exception as exc:
        err_msg = str(exc)
        job_store.fail_publishing(job_id, err_msg)
        storage_cleaner.cleanup_job_local_files(job_id)
        sys_alert = failure_tracker.record_failure("instagram_publish", job_id, err_msg)
        logger.error("[%s] Instagram publishing failed: %s", job_id, err_msg, exc_info=True)
        fail_msg = (
            f"❌ **Instagram Publish Failed**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Reason**: {err_msg}\n\n"
            f"Review logs or use `/retry {job_id}`."
        )
        if sys_alert:
            fail_msg += f"\n\n{sys_alert}"
        await _update_progress_message(status_msg, fail_msg)
    finally:
        task_registry.unregister_task(job_id)


async def _handle_carousel_pipeline(
    job_id: str,
    result: DownloadResult,
    status_msg: Message,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Handles multi-slide Instagram carousel posts (videos, images, or mixed)."""
    items = result.carousel_items or []
    total = len(items)
    logger.info("[%s] Beginning carousel pipeline with %d slides...", job_id, total)

    try:
        await _update_progress_message(
            status_msg,
            f"⚡ **Processing Carousel Post...**\n\n"
            f"📸 **{result.title}**\n\n"
            f"Found {total} slides. Preparing media for Instagram...",
        )

        uploaded_items: List[Dict[str, str]] = []

        for idx, item in enumerate(items, start=1):
            item_type = item.get("type", "video")
            source_path = Path(item["file_path"])

            if item_type == "video":
                # Video slide: serialize through render_queue
                async def on_queue_wait(position: int) -> None:
                    wait_text = (
                        f"⏳ **Waiting in Render Queue...**\n\n"
                        f"📸 Slide {idx}/{total} (Video)\n\n"
                        f"Position: `#{position}` in queue"
                    )
                    await _update_progress_message(status_msg, wait_text)

                await _update_progress_message(
                    status_msg,
                    f"⚡ **Processing Carousel ({idx}/{total})...**\n\n"
                    f"🎬 Optimizing video slide {idx}...",
                )

                processed_slide_path = PROCESSED_DIR / f"{job_id}_slide_{idx}.mp4"
                async with render_queue.acquire_slot(f"{job_id}_slide_{idx}", on_wait_callback=on_queue_wait):
                    await video_processor.process_video(
                        job_id=job_id,
                        preset="balanced",
                        input_file=source_path,
                        output_file=processed_slide_path,
                        update_job_store=False,
                    )

                # Upload video slide to Azure Blob Storage
                await _update_progress_message(
                    status_msg,
                    f"☁️ **Uploading Carousel ({idx}/{total})...**\n\n"
                    f"Uploading video slide {idx} to cloud storage...",
                )
                upload_data = await azure_storage_manager.upload_media_blob(
                    job_id=job_id,
                    file_path=str(processed_slide_path),
                    blob_name=f"{job_id}_slide_{idx}.mp4",
                    content_type="video/mp4",
                )
                uploaded_items.append({
                    "url": upload_data["sas_url"],
                    "type": "VIDEO",
                })

            else:
                # Image slide: optimize via Pillow as JPEG RGB
                await _update_progress_message(
                    status_msg,
                    f"⚡ **Processing Carousel ({idx}/{total})...**\n\n"
                    f"🖼️ Optimizing image slide {idx}...",
                )
                processed_img_path = PROCESSED_DIR / f"{job_id}_slide_{idx}.jpg"
                try:
                    from PIL import Image
                    with Image.open(source_path) as img:
                        rgb_img = img.convert("RGB")
                        rgb_img.save(processed_img_path, format="JPEG", quality=95, optimize=True)
                except Exception as img_err:
                    logger.warning("[%s] Pillow processing failed for slide %d: %s. Copying original...", job_id, idx, img_err)
                    shutil.copy2(source_path, processed_img_path)

                await _update_progress_message(
                    status_msg,
                    f"☁️ **Uploading Carousel ({idx}/{total})...**\n\n"
                    f"Uploading image slide {idx} to cloud storage...",
                )
                upload_data = await azure_storage_manager.upload_media_blob(
                    job_id=job_id,
                    file_path=str(processed_img_path),
                    blob_name=f"{job_id}_slide_{idx}.jpg",
                    content_type="image/jpeg",
                )
                uploaded_items.append({
                    "url": upload_data["sas_url"],
                    "type": "IMAGE",
                })

        # All slides processed and uploaded to Azure!
        job = job_store.get_job(job_id)
        caption = (job.get("caption") if job else None) or job_store.get_setting("default_caption") or DEFAULT_MEME_CAPTION

        await _update_progress_message(
            status_msg,
            f"⚡ **Publishing Carousel...**\n\n"
            f"📸 **{result.title}** ({total} slides)\n\n"
            f"🚀 Publishing to Instagram...",
        )
        asyncio.create_task(
            _run_instagram_carousel_publish_background(
                job_id=job_id,
                items=uploaded_items,
                caption=caption,
                status_msg=status_msg,
                title=result.title,
            )
        )

    except asyncio.CancelledError:
        logger.info("[%s] Carousel pipeline was cancelled.", job_id)
        storage_cleaner.cleanup_job_local_files(job_id)
        raise
    except Exception as exc:
        err_msg = str(exc)
        job_store.fail_processing(job_id, err_msg)
        storage_cleaner.cleanup_job_local_files(job_id)
        logger.error("[%s] Carousel processing pipeline failed: %s", job_id, err_msg, exc_info=True)
        await _update_progress_message(
            status_msg,
            f"❌ **Carousel Processing Failed**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Reason**: {err_msg[:200]}\n\n"
            f"Review logs or use `/retry {job_id}`.",
        )


async def _run_instagram_carousel_publish_background(
    job_id: str,
    items: List[Dict[str, str]],
    caption: str,
    status_msg: Message,
    title: str = "Instagram Carousel",
) -> None:
    """Executes the multi-slide Instagram carousel publishing pipeline."""
    current_task = asyncio.current_task()
    if current_task:
        task_registry.register_task(job_id, current_task)

    job_store.start_publishing(job_id, caption=caption)
    last_update_time = 0.0

    async def publish_progress(status_code: str, elapsed: float) -> None:
        nonlocal last_update_time
        now = time.monotonic()
        if now - last_update_time >= 3.0:
            last_update_time = now
            msg_text = (
                f"⚡ **Publishing Carousel...**\n\n"
                f"📸 **{title}** ({len(items)} slides)\n\n"
                f"🚀 Rendering on Instagram ({int(elapsed)}s)..."
            )
            await _update_progress_message(status_msg, msg_text)

    try:
        publish_res = await instagram_publisher.publish_carousel(
            job_id=job_id,
            items=items,
            caption=caption,
            progress_callback=publish_progress,
        )

        storage_cleaner.cleanup_job_local_files(job_id)
        failure_tracker.record_success("instagram_publish")

        permalink = publish_res.get("permalink", "https://instagram.com")
        success_msg = (
            f"🎉 **Carousel Published!**\n\n"
            f"📸 **{title}** ({len(items)} slides)\n\n"
            f"👉 [Watch on Instagram]({permalink})"
        )
        await _update_progress_message(status_msg, success_msg)
        logger.info("[%s] Instagram Carousel successfully published: %s", job_id, permalink)

    except asyncio.CancelledError:
        logger.info("[%s] Instagram carousel publish task was cancelled.", job_id)
        storage_cleaner.cleanup_job_local_files(job_id)
        raise

    except InstagramTokenExpiredError as token_err:
        job_store.fail_publishing(job_id, str(token_err))
        storage_cleaner.cleanup_job_local_files(job_id)
        sys_alert = failure_tracker.record_failure("token", job_id, str(token_err))
        alert_msg = (
            f"🚨 **CRITICAL: Instagram Access Token Expired!**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Error**: Meta rejected the access token as expired or invalid.\n\n"
            f"⚠️ **Action Required**: Please generate a new 60-day long-lived access token."
        )
        if sys_alert:
            alert_msg += f"\n\n{sys_alert}"
        await _update_progress_message(status_msg, alert_msg)

    except InstagramRateLimitError as rate_err:
        job_store.fail_publishing(job_id, str(rate_err))
        storage_cleaner.cleanup_job_local_files(job_id)
        sys_alert = failure_tracker.record_failure("instagram_publish", job_id, str(rate_err))
        rate_msg = (
            f"⏳ **Instagram Rate Limit Reached**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Reason**: {rate_err}\n\n"
            f"Please wait before publishing more posts."
        )
        if sys_alert:
            rate_msg += f"\n\n{sys_alert}"
        await _update_progress_message(status_msg, rate_msg)

    except Exception as exc:
        err_msg = str(exc)
        job_store.fail_publishing(job_id, err_msg)
        storage_cleaner.cleanup_job_local_files(job_id)
        sys_alert = failure_tracker.record_failure("instagram_publish", job_id, err_msg)
        logger.error("[%s] Instagram carousel publishing failed: %s", job_id, err_msg, exc_info=True)
        fail_msg = (
            f"❌ **Instagram Carousel Publish Failed**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Reason**: {err_msg}\n\n"
            f"Review logs or use `/retry {job_id}`."
        )
        if sys_alert:
            fail_msg += f"\n\n{sys_alert}"
        await _update_progress_message(status_msg, fail_msg)
    finally:
        task_registry.unregister_task(job_id)


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

    # 1. Check if user was prompted to send a new default caption via /caption or button
    user_data = getattr(context, "user_data", None)
    if isinstance(user_data, dict) and user_data.get("awaiting") == "caption":
        new_caption = message.text.strip()
        job_store.set_setting("default_caption", new_caption)
        user_data["awaiting"] = None
        await message.reply_text(
            f"✅ **Default caption updated successfully!**\n\n"
            f"📝 **New Default Template:**\n{new_caption}\n\n"
            f"💡 Future Reels sent without a caption will automatically use this template.",
            parse_mode="Markdown",
        )
        return

    # 2. Check if user is sending pasted cookies text (prompted or unprompted)
    raw_input = message.text.strip()
    is_cookie_text = (
        (isinstance(user_data, dict) and user_data.get("awaiting") == "cookies")
        or raw_input.startswith(("# Netscape", "# HTTP Cookie File"))
        or (".youtube.com\t" in raw_input)
    )
    if is_cookie_text:
        if not ("youtube.com" in raw_input or "google.com" in raw_input or "# Netscape" in raw_input or "\t" in raw_input):
            await message.reply_text(
                "❌ **Invalid Cookies Text**\n\n"
                "The text does not appear to contain Netscape-format YouTube cookies.\n"
                "Please make sure it has tab-separated cookie entries.",
                parse_mode="Markdown",
            )
            return

        target_path = BASE_DIR / "cookies.txt"
        target_path.write_text(raw_input, encoding="utf-8")

        has_auth = any(token in raw_input for token in ["LOGIN_INFO", "__Secure-3PSID", "SAPISID", "SID"])
        size_kb = len(raw_input.encode("utf-8")) / 1024.0

        if isinstance(user_data, dict):
            user_data["awaiting"] = None

        auth_badge = "✅ Authenticated (Active login session)" if has_auth else "⚠️ Warning: No login tokens detected (Guest session)"
        await message.reply_text(
            f"🍪 **YouTube Cookies Updated Successfully!**\n\n"
            f"• **Method**: Pasted text saved to `{target_path.name}`\n"
            f"• **File Size**: `{size_kb:.1f} KB`\n"
            f"• **Session**: {auth_badge}\n\n"
            f"✨ All future YouTube downloads will use these cookies immediately.",
            parse_mode="Markdown",
        )
        logger.info("Successfully updated cookies.txt via pasted text (size: %.1f KB, authenticated=%s)", size_kb, has_auth)
        return

    match = MEDIA_URL_REGEX.search(message.text)
    if not match:
        # Check if the user is replying with a caption for an active job
        awaiting_job = job_store.get_active_awaiting_caption_job(user_id)
        if awaiting_job:
            caption_text = message.text.strip()
            # If user sent /skip, default to the original video title
            if caption_text.lower() == "/skip":
                caption_text = awaiting_job.get("title") or "New Post"

            job_id = awaiting_job["job_id"]
            status_msg = await message.reply_text(
                "🚀 **Publishing...**\n\n"
                "Sending to Instagram...",
                parse_mode="Markdown",
            )
            asyncio.create_task(_run_instagram_publish_background(job_id, caption_text, status_msg))
            return
        return

    url = match.group(1).strip()
    is_ig = "instagram.com" in url.lower() or "instagr.am" in url.lower()
    platform_name = "Instagram" if is_ig else "YouTube"
    post_type_label = "Post" if is_ig else "Reel"

    # Extract inline caption (everything other than the URL)
    # Allows sending: https://youtube.com/watch?v=xyz My multiline caption here
    raw_text = message.text
    caption_part = raw_text.replace(match.group(0), "").strip()
    inline_caption = caption_part if caption_part else None

    # If no inline caption provided, use the configured default caption template!
    final_caption = inline_caption
    if not final_caption:
        final_caption = job_store.get_setting("default_caption") or DEFAULT_MEME_CAPTION

    # 1. Generate unique Job ID (UUID4)
    job_id = str(uuid.uuid4())
    logger.info(
        "New download job registered: job_id=%s, user_id=%s, url=%s, platform=%s, has_inline_caption=%s",
        job_id,
        user_id,
        url,
        platform_name,
        bool(inline_caption),
    )

    # 2. Persist in SQLite Job Store (with final caption stored so it auto-publishes)
    job_store.create_job(job_id=job_id, user_id=user_id, source_url=url, caption=final_caption)

    # 3. Send immediate acknowledgment message
    caption_note = " (with custom caption)" if inline_caption else ""
    ack_text = (
        f"⚡ **Processing {platform_name} {post_type_label}{caption_note}...**\n\n"
        f"📥 Connecting to {platform_name}..."
    )
    status_msg = await message.reply_text(ack_text, parse_mode="Markdown")

    # 4. Start background download task without blocking the polling event loop
    loop = asyncio.get_running_loop()
    asyncio.create_task(_run_download_background(job_id, url, status_msg, loop))


# Alias for clarity
media_url_handler = youtube_url_handler


async def _resume_azure_upload(
    job_id: str,
    processed_path: str,
    status_msg: Optional[Message],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Resumes the pipeline directly from Azure upload stage, skipping download and processing."""
    current_task = asyncio.current_task()
    if current_task:
        task_registry.register_task(job_id, current_task)

    job_store.start_azure_upload(job_id)
    if status_msg:
        await _update_progress_message(
            status_msg,
            "⚡ **Resuming Upload...**\n\n"
            "☁️ Uploading to cloud storage...",
        )

    try:
        await azure_storage_manager.ensure_cover_uploaded(config.default_cover_path)
        video_data = await azure_storage_manager.upload_video_blob(
            job_id=job_id,
            file_path=processed_path,
        )

        failure_tracker.record_success("azure_upload")
        job_store.complete_azure_upload(
            job_id=job_id,
            blob_name=video_data["blob_name"],
            sas_url=video_data["sas_url"],
            expires_at=video_data["expires_at"],
        )

        job = job_store.get_job(job_id)
        existing_caption = job.get("caption") if job else None

        if existing_caption:
            if status_msg:
                title = job.get("title") or "Instagram Reel"
                await _update_progress_message(
                    status_msg,
                    f"⚡ **Publishing Reel...**\n\n"
                    f"🎬 **{title}**\n\n"
                    f"🚀 Publishing to Instagram...",
                )
            asyncio.create_task(_run_instagram_publish_background(job_id, existing_caption, status_msg))
        else:
            job_store.set_awaiting_caption(job_id)
            if status_msg:
                title = job.get("title") or "Video"
                prompt_msg = (
                    f"🎬 **Video Ready!**\n\n"
                    f"**{title}**\n\n"
                    f"💬 Send the caption for this Reel, or `/skip` to publish directly."
                )
                await _update_progress_message(status_msg, prompt_msg)

    except asyncio.CancelledError:
        logger.info("[%s] Resumed Azure upload task cancelled.", job_id)
        storage_cleaner.cleanup_job_local_files(job_id)
        raise
    except Exception as exc:
        err_text = str(exc)
        clean_err = re.sub(r"^\[[A-Z_]+\]\s*", "", err_text)
        job_store.fail_azure_upload(job_id, err_text)
        sys_alert = failure_tracker.record_failure("azure_upload", job_id, err_text)
        storage_cleaner.cleanup_job_local_files(job_id)
        logger.error("[%s] Resumed Azure upload failed: %s", job_id, err_text)
        if status_msg:
            fail_msg = f"❌ **Azure Hosting Failed**: {clean_err}\n\nUse `/retry {job_id}` to try again."
            if sys_alert:
                fail_msg += f"\n\n{sys_alert}"
            await _update_progress_message(status_msg, fail_msg)
    finally:
        task_registry.unregister_task(job_id)


async def _resume_processing(
    job_id: str,
    download_path: str,
    status_msg: Optional[Message],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Resumes the pipeline directly from processing stage, skipping download."""
    current_task = asyncio.current_task()
    if current_task:
        task_registry.register_task(job_id, current_task)

    job_store.update_status(job_id, "processing")
    job = job_store.get_job(job_id)
    title = job.get("title") or "Video"
    if status_msg:
        await _update_progress_message(
            status_msg,
            f"⚡ **Processing Reel...**\n\n"
            f"🎬 **{title}**\n\n"
            f"⚙️ Optimizing video format...",
        )

    last_proc_edit_time = 0.0
    MIN_EDIT_INTERVAL = 1.8

    def proc_progress_callback(pct: int, curr_sec: float, total_sec: float) -> None:
        nonlocal last_proc_edit_time
        now = time.time()
        if status_msg and (now - last_proc_edit_time >= MIN_EDIT_INTERVAL):
            last_proc_edit_time = now
            bar_len = 10
            filled = int(bar_len * pct / 100)
            bar = "█" * filled + "░" * (bar_len - filled)
            p_text = (
                f"⚡ **Processing Reel...**\n\n"
                f"🎬 **{title}**\n\n"
                f"✂️ **Rendering**: `[{bar}] {pct}%`"
            )
            asyncio.run_coroutine_threadsafe(
                _update_progress_message(status_msg, p_text),
                loop,
            )

    try:
        proc_result = await video_processor.process_video(
            job_id=job_id,
            preset="balanced",
            progress_callback=proc_progress_callback,
        )

        failure_tracker.record_success("processing")
        await _resume_azure_upload(job_id, proc_result.file_path, status_msg, loop)

    except asyncio.CancelledError:
        logger.info("[%s] Resumed processing task cancelled.", job_id)
        storage_cleaner.cleanup_job_local_files(job_id)
        raise
    except Exception as exc:
        err_msg = str(exc)
        job_store.fail_processing(job_id, err_msg)
        sys_alert = failure_tracker.record_failure("processing", job_id, err_msg)
        storage_cleaner.cleanup_job_local_files(job_id)
        logger.error("[%s] Resumed processing failed: %s", job_id, err_msg)
        if status_msg:
            fail_msg = f"❌ **Processing Failed**: {err_msg}\n\nUse `/retry {job_id}` to try again."
            if sys_alert:
                fail_msg += f"\n\n{sys_alert}"
            await _update_progress_message(status_msg, fail_msg)
    finally:
        task_registry.unregister_task(job_id)


# =====================================================================
# Stage 7: Operational Commands (/jobs, /job, /cancel, /retry)
# =====================================================================

@restricted
async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists all active and in-progress jobs with elapsed durations."""
    in_progress = job_store.list_in_progress_jobs()
    if not in_progress:
        if update.effective_message:
            await update.effective_message.reply_text("📋 **No jobs currently in progress.**", parse_mode="Markdown")
        return

    now = datetime.now(timezone.utc)
    lines = [f"📋 **Active In-Progress Jobs ({len(in_progress)})**:\n"]

    for job in in_progress:
        jid = job["job_id"]
        st = job["status"]
        title = job.get("title") or job.get("source_url") or "Unknown"

        updated_at_str = job.get("updated_at") or job.get("created_at")
        elapsed_str = ""
        if updated_at_str:
            try:
                dt = datetime.fromisoformat(updated_at_str)
                secs = max(0, int((now - dt).total_seconds()))
                if secs < 60:
                    elapsed_str = f"{secs}s"
                elif secs < 3600:
                    elapsed_str = f"{secs // 60}m {secs % 60}s"
                else:
                    elapsed_str = f"{secs // 3600}h {(secs % 3600) // 60}m"
            except Exception:
                pass

        time_part = f" ({elapsed_str} in status)" if elapsed_str else ""
        lines.append(
            f"• `{jid[:8]}...` — **{st}**{time_part}\n"
            f"  _{title[:45]}_\n"
            f"  Inspect: `/job {jid}` | Cancel: `/cancel {jid}`"
        )

    if update.effective_message:
        await update.effective_message.reply_text("\n\n".join(lines), parse_mode="Markdown")


@restricted
async def job_detail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays detailed telemetry for a specific job."""
    if not context.args:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: `/job <job_id>`", parse_mode="Markdown")
        return

    job_id = context.args[0].strip()
    job = job_store.get_job(job_id)
    if not job:
        if update.effective_message:
            await update.effective_message.reply_text(f"❌ Job `{job_id}` not found.", parse_mode="Markdown")
        return

    from bot.db import DOWNLOADS_DIR, PROCESSED_DIR
    raw_exists = (DOWNLOADS_DIR / f"{job_id}.mp4").exists()
    proc_exists = (PROCESSED_DIR / f"{job_id}.mp4").exists()

    blob_name = job.get("video_blob_name") or "None"
    sas_expiry = job.get("video_sas_expires_at") or "None"
    err = job.get("error_message") or "None"
    media_id = job.get("instagram_media_id") or "None"
    permalink = job.get("instagram_permalink") or "None"
    caption = job.get("caption")
    caption_preview = f"_{caption[:60]}..._" if caption else "None"

    is_running = task_registry.is_task_running(job_id)

    report = (
        f"🔍 **Job Details: `{job_id}`**\n\n"
        f"• **Status**: `{job['status']}` {'(🟢 Active Task)' if is_running else ''}\n"
        f"• **Title**: {job.get('title') or 'N/A'}\n"
        f"• **Source URL**: {job.get('source_url') or 'N/A'}\n"
        f"• **Raw Download On Disk**: `{'Yes' if raw_exists else 'No'}`\n"
        f"• **Processed Master On Disk**: `{'Yes' if proc_exists else 'No'}`\n"
        f"• **Azure Blob**: `{blob_name}`\n"
        f"• **SAS Expiry**: `{sas_expiry}`\n"
        f"• **Instagram Media ID**: `{media_id}`\n"
        f"• **Instagram Link**: {permalink}\n"
        f"• **Caption**: {caption_preview}\n"
        f"• **Error**: `{err}`\n"
        f"• **Created**: `{job.get('created_at')}`\n"
        f"• **Updated**: `{job.get('updated_at')}`\n\n"
        f"Actions: `/retry {job_id}` | `/cancel {job_id}`"
    )
    if update.effective_message:
        await update.effective_message.reply_text(report, parse_mode="Markdown")


@restricted
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels an active or pending job and purges local files, or cancels pending prompt."""
    user_data = getattr(context, "user_data", None)
    if isinstance(user_data, dict) and user_data.get("awaiting"):
        user_data["awaiting"] = None
        if update.effective_message:
            await update.effective_message.reply_text("🛑 Cancelled caption/cover update mode.")
        return

    if not context.args:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: `/cancel <job_id>`", parse_mode="Markdown")
        return

    job_id = context.args[0].strip()
    job = job_store.get_job(job_id)
    if not job:
        if update.effective_message:
            await update.effective_message.reply_text(f"❌ Job `{job_id}` not found.", parse_mode="Markdown")
        return

    # Check if already published
    if job.get("status") == "published":
        if update.effective_message:
            await update.effective_message.reply_text(
                f"⚠️ Cannot cancel job `{job_id}`: Reel is already published on Instagram live!",
                parse_mode="Markdown",
            )
        return

    # Stop active background task if running
    task_cancelled = task_registry.cancel_task(job_id)

    # Cancel in database
    ok, msg = job_store.cancel_job(job_id)
    storage_cleaner.cleanup_job_local_files(job_id)

    # Clean Azure blob if uploaded
    blob_name = job.get("video_blob_name")
    if blob_name:
        try:
            await azure_storage_manager.delete_video_blob(blob_name)
        except Exception as exc:
            logger.debug("[%s] Azure blob deletion note on cancel: %s", job_id, exc)

    reply_text = (
        f"🛑 **Job Cancelled**\n\n"
        f"• **Job ID**: `{job_id}`\n"
        f"• **Running Task Cancelled**: `{'Yes' if task_cancelled else 'No'}`\n"
        f"• **Local Storage**: Purged\n"
        f"• **Status**: `cancelled`"
    )
    if update.effective_message:
        await update.effective_message.reply_text(reply_text, parse_mode="Markdown")


@restricted
async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Intelligently resumes a failed or interrupted job from the last valid stage."""
    if not context.args:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: `/retry <job_id>`", parse_mode="Markdown")
        return

    job_id = context.args[0].strip()
    job = job_store.get_job(job_id)
    if not job:
        if update.effective_message:
            await update.effective_message.reply_text(f"❌ Job `{job_id}` not found.", parse_mode="Markdown")
        return

    if job.get("status") == "published":
        if update.effective_message:
            await update.effective_message.reply_text(
                f"ℹ️ Job `{job_id}` is already published to Instagram live!",
                parse_mode="Markdown",
            )
        return

    if task_registry.is_task_running(job_id):
        if update.effective_message:
            await update.effective_message.reply_text(
                f"⚠️ Job `{job_id}` already has an active background task running.",
                parse_mode="Markdown",
            )
        return

    can_resume, stage, _ = job_store.get_job_resumption_stage(job_id)
    if not can_resume:
        if update.effective_message:
            await update.effective_message.reply_text(f"❌ Cannot resume job `{job_id}`: {stage}", parse_mode="Markdown")
        return

    status_msg = None
    if update.effective_message:
        status_msg = await update.effective_message.reply_text(
            f"🔄 **Resuming Job `{job_id[:8]}...`**\n\n"
            f"• **Resumption Stage**: `{stage}`\n"
            f"• **Source**: {job.get('source_url')}\n"
            f"• Initializing pipeline...",
            parse_mode="Markdown",
        )

    loop = asyncio.get_running_loop()

    if stage == "publish":
        caption = job.get("caption") or job.get("title") or "New Reel"
        job_store.reset_job_status(job_id, "hosted")
        asyncio.create_task(_run_instagram_publish_background(job_id, caption, status_msg))

    elif stage == "upload_to_azure":
        from bot.db import PROCESSED_DIR
        processed_file_path = str(PROCESSED_DIR / f"{job_id}.mp4")
        job_store.reset_job_status(job_id, "processed")
        asyncio.create_task(_resume_azure_upload(job_id, processed_file_path, status_msg, loop))

    elif stage == "process":
        from bot.db import DOWNLOADS_DIR
        download_path = str(DOWNLOADS_DIR / f"{job_id}.mp4")
        job_store.reset_job_status(job_id, "downloaded")
        asyncio.create_task(_resume_processing(job_id, download_path, status_msg, loop))

    else:
        job_store.reset_job_status(job_id, "pending")
        asyncio.create_task(_run_download_background(job_id, job["source_url"], status_msg, loop))


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global error handler for unhandled exceptions in any handler.
    Logs the error with traceback and notifies the user without crashing the bot process.
    """
    logger.error(
        "Unhandled exception while processing Telegram update: %s",
        context.error,
        exc_info=context.error,
    )

    eff_msg = getattr(update, "effective_message", None) if update else None
    if eff_msg:
        try:
            await eff_msg.reply_text(
                "⚠️ **An unexpected error occurred while processing this request.**\n\n"
                "The bot is continuing to run and the error has been logged for review.",
                parse_mode="Markdown",
            )
        except Exception as notify_exc:
            logger.debug("Failed to send error notice to chat: %s", notify_exc)


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

