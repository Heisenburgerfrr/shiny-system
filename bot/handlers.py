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
from typing import Any, Callable, Dict, List, Optional, Tuple

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

# Regex to detect Instagram URLs (reels, p/posts, tv, share links, with optional username segment)
INSTAGRAM_URL_REGEX = re.compile(
    r"(https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:(?:share/)?(?:p|reel|reels|tv)/|(?:(?!direct/|stories/|explore/)[^/?#\s]+/)?(?:p|reel|reels|tv)/)[a-zA-Z0-9_\-]+[^\s]*)",
    re.IGNORECASE,
)

# Unified Media URL Regex matching either YouTube or Instagram
MEDIA_URL_REGEX = re.compile(
    r"(https?://(?:www\.)?(?:youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/|embed/)|youtu\.be/|(?:instagram\.com|instagr\.am)/(?:(?:share/)?(?:p|reel|reels|tv)/|(?:(?!direct/|stories/|explore/)[^/?#\s]+/)?(?:p|reel|reels|tv)/))[a-zA-Z0-9_\-]+[^\s]*)",
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


def is_caption_enabled() -> bool:
    """Returns True if custom default caption template feature is enabled, False otherwise (default: False/off)."""
    return job_store.get_setting("caption_enabled", "false").lower() in ("true", "1", "yes", "on")


def _build_caption_components() -> Tuple[str, InlineKeyboardMarkup]:
    """Builds message text and inline keyboard for the /caption dashboard."""
    enabled = is_caption_enabled()
    current = job_store.get_setting("default_caption") or DEFAULT_MEME_CAPTION

    if enabled:
        status_line = "🟢 **Feature Status**: **TURNED ON**\n*(Custom template will be applied to every Reel sent without an inline caption)*"
        toggle_button = InlineKeyboardButton("🔴 Turn off this feature", callback_data="toggle_caption_off")
    else:
        status_line = "⚪ **Feature Status**: **TURNED OFF** (Default)\n*(Reels will automatically use the original video title from YouTube/Instagram)*"
        toggle_button = InlineKeyboardButton("🟢 Turn on this feature", callback_data="toggle_caption_on")

    keyboard = InlineKeyboardMarkup([
        [toggle_button],
        [InlineKeyboardButton("✏️ Change Default Caption", callback_data="change_caption")],
    ])

    text = (
        f"📝 **Default Caption / Title Settings**\n\n"
        f"{status_line}\n\n"
        f"📋 **Current Template:**\n"
        f"{current}\n\n"
        f"💡 *Toggle above to enable/disable. When turned off, Reels automatically use the video's original title.*"
    )
    return text, keyboard


@restricted
async def caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles /caption: displays the current default caption template,
    with an interactive toggle button to turn the feature on or off.
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
            text, keyboard = _build_caption_components()
            await message.reply_text(
                f"✅ **Default caption updated!**\n\n{text}",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            return

    text, keyboard = _build_caption_components()
    await message.reply_text(
        text,
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


def is_cover_enabled() -> bool:
    """Returns True if custom Reels cover image feature is enabled, False otherwise (default: False/off)."""
    return job_store.get_setting("cover_enabled", "false").lower() in ("true", "1", "yes", "on")


def _build_cover_components() -> Tuple[str, InlineKeyboardMarkup, bool]:
    """Builds caption text, inline keyboard, and photo validity flag for /cover dashboard."""
    enabled = is_cover_enabled()
    cover_path = config.default_cover_path
    cover_info = inspect_cover_image(cover_path)

    if enabled:
        status_line = "🟢 **Feature Status**: **TURNED ON**\n*(Custom cover will be applied to every Reel)*"
        toggle_button = InlineKeyboardButton("🔴 Turn off this feature", callback_data="toggle_cover_off")
    else:
        status_line = "⚪ **Feature Status**: **TURNED OFF** (Default)\n*(Instagram will use the video's native thumbnail)*"
        toggle_button = InlineKeyboardButton("🟢 Turn on this feature", callback_data="toggle_cover_on")

    keyboard = InlineKeyboardMarkup([
        [toggle_button],
        [InlineKeyboardButton("🖼️ Change Cover Image", callback_data="change_cover")],
    ])

    if cover_path.exists() and cover_info.is_valid:
        text = (
            f"🖼️ **Reels Cover Settings**\n\n"
            f"{status_line}\n\n"
            f"• **Dimensions**: `{cover_info.width}x{cover_info.height}` ({cover_info.format})\n"
            f"• **Aspect Ratio**: 9:16 (Instagram Reels compliant)\n"
            f"• **File Size**: `{cover_info.file_size / 1024:.1f} KB`\n\n"
            f"💡 *Toggle above to enable/disable. When turned off, Reels automatically use the video's own thumbnail.*"
        )
        has_valid_photo = True
    else:
        text = (
            f"🖼️ **Reels Cover Settings**\n\n"
            f"{status_line}\n\n"
            f"• **Status**: `{cover_info.status_summary}` ({cover_info.details})\n\n"
            f"💡 *Toggle above to enable/disable. When turned off, Reels automatically use the video's own thumbnail.*"
        )
        has_valid_photo = False

    return text, keyboard, has_valid_photo


@restricted
async def cover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles /cover: displays the current Reels cover image with resolution, specs,
    and interactive toggle button to turn the feature on or off.
    """
    message = update.effective_message
    if not message:
        return

    text, keyboard, has_valid_photo = _build_cover_components()
    cover_path = config.default_cover_path

    if has_valid_photo:
        with open(cover_path, "rb") as photo_f:
            await message.reply_photo(
                photo=photo_f,
                caption=text,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
    else:
        await message.reply_text(
            text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )


def _merge_cookies(existing_content: str, new_content: str) -> str:
    """Merges new cookies into existing cookies preserving other domains."""
    if not existing_content.strip():
        return new_content
    new_domains = set()
    for line in new_content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            parts = line.split("\t")
            if parts:
                new_domains.add(parts[0].lstrip("."))

    merged_lines = []
    for line in existing_content.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.strip().split("\t")
        if parts:
            domain = parts[0].lstrip(".")
            if not any(nd in domain or domain in nd for nd in new_domains):
                merged_lines.append(line.strip())

    return "# Netscape HTTP Cookie File\n" + "\n".join(merged_lines) + "\n" + new_content.strip() + "\n"


def _remove_cookie_domains(content: str, domains_to_remove: Tuple[str, ...]) -> str:
    """Removes cookies matching specified domain keywords while keeping other cookies intact."""
    kept_lines = []
    for line in content.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.strip().split("\t")
        if parts:
            domain = parts[0].lower()
            if any(d in domain for d in domains_to_remove):
                continue
            kept_lines.append(line.strip())
    if kept_lines:
        return "# Netscape HTTP Cookie File\n" + "\n".join(kept_lines) + "\n"
    return ""


def _format_cookies_badge(content: str) -> Tuple[str, str]:
    has_yt = any(token in content for token in ["LOGIN_INFO", "__Secure-3PSID", "SAPISID", "SID"])
    has_ig = any(token in content for token in ["sessionid", "ds_user_id", "csrftoken"])
    yt_badge = "✅ Authenticated" if has_yt else "⚠️ Missing / Guest"
    ig_badge = "✅ Authenticated" if has_ig else "⚠️ Missing / Guest"
    return yt_badge, ig_badge


def _build_cookies_dashboard() -> Tuple[str, InlineKeyboardMarkup]:
    cookies_path = BASE_DIR / "cookies.txt"
    has_file = cookies_path.is_file()
    size_kb = (cookies_path.stat().st_size / 1024.0) if has_file else 0.0
    content = cookies_path.read_text(encoding="utf-8", errors="ignore") if has_file else ""
    yt_badge, ig_badge = _format_cookies_badge(content)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 Update YouTube", callback_data="cookies_pick_yt"),
            InlineKeyboardButton("📸 Update Instagram", callback_data="cookies_pick_ig"),
        ],
        [
            InlineKeyboardButton("🔄 Replace Both", callback_data="cookies_pick_all"),
            InlineKeyboardButton("🔄 Refresh Status", callback_data="cookies_refresh"),
        ],
        [
            InlineKeyboardButton("🗑 Clear YouTube", callback_data="cookies_clear_yt"),
            InlineKeyboardButton("🗑 Clear Instagram", callback_data="cookies_clear_ig"),
        ],
    ])

    if has_file and size_kb > 0:
        msg = (
            f"🍪 **Cookies Control Panel**\n\n"
            f"• **Location**: `{cookies_path.name}` ({size_kb:.1f} KB)\n"
            f"• **YouTube Session**: {yt_badge}\n"
            f"• **Instagram Session**: {ig_badge}\n\n"
            f"👉 **Choose which one to update or manage:**\n"
            f"Updating YouTube or Instagram individually will **never** affect the other platform!\n\n"
            f"💡 *You can also drag-and-drop a `cookies.txt` file or paste text directly anytime.*"
        )
    else:
        msg = (
            f"🍪 **Cookies Control Panel**\n\n"
            f"⚠️ `cookies.txt` is not currently present on the server.\n\n"
            f"• **YouTube Session**: {yt_badge}\n"
            f"• **Instagram Session**: {ig_badge}\n\n"
            f"👉 Select which platform's cookies you want to add below, or upload a `cookies.txt` file directly."
        )
    return msg, keyboard


@restricted
async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles /cookies: displays interactive control panel for YouTube and Instagram cookies,
    or immediately updates cookies if text was passed (/cookies <pasted lines>).
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
            existing_text = target_path.read_text(encoding="utf-8", errors="ignore") if target_path.is_file() else ""
            merged_text = _merge_cookies(existing_text, raw_text)
            target_path.write_text(merged_text, encoding="utf-8")

            yt_badge, ig_badge = _format_cookies_badge(merged_text)
            size_kb = len(merged_text.encode("utf-8")) / 1024.0
            await message.reply_text(
                f"🍪 **Cookies Updated Successfully!**\n\n"
                f"• **Method**: Direct argument merged into `{target_path.name}`\n"
                f"• **File Size**: `{size_kb:.1f} KB`\n"
                f"• **YouTube**: {yt_badge}\n"
                f"• **Instagram**: {ig_badge}\n\n"
                f"✨ All future downloads (YouTube & Instagram) will use these cookies immediately.",
                parse_mode="Markdown",
            )
            return

    msg, keyboard = _build_cookies_dashboard()
    await message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")


@restricted
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline button clicks for captions, covers, and selective cookie management."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if query.data == "toggle_caption_on":
        job_store.set_setting("caption_enabled", "true")
        await query.answer("🟢 Custom caption template turned ON!", show_alert=False)
        text, keyboard = _build_caption_components()
        try:
            await query.message.edit_text(text=text, reply_markup=keyboard, parse_mode="Markdown")
        except Exception as edit_err:
            logger.debug("Failed to edit caption message inline: %s", edit_err)
    elif query.data == "toggle_caption_off":
        job_store.set_setting("caption_enabled", "false")
        await query.answer("⚪ Custom caption template turned OFF! (Reels will use video title)", show_alert=False)
        text, keyboard = _build_caption_components()
        try:
            await query.message.edit_text(text=text, reply_markup=keyboard, parse_mode="Markdown")
        except Exception as edit_err:
            logger.debug("Failed to edit caption message inline: %s", edit_err)
    elif query.data == "change_caption":
        context.user_data["awaiting"] = "caption"
        await query.message.reply_text(
            "✍️ **Send your new default caption now.**\n\n"
            "Simply send your desired caption as a text message, and it will be saved as the default template for all future Reels.",
            parse_mode="Markdown",
        )
    elif query.data == "toggle_cover_on":
        job_store.set_setting("cover_enabled", "true")
        await query.answer("🟢 Custom cover feature turned ON!", show_alert=False)
        text, keyboard, _ = _build_cover_components()
        try:
            if query.message.photo:
                await query.message.edit_caption(caption=text, reply_markup=keyboard, parse_mode="Markdown")
            else:
                await query.message.edit_text(text=text, reply_markup=keyboard, parse_mode="Markdown")
        except Exception as edit_err:
            logger.debug("Failed to edit cover message inline: %s", edit_err)
    elif query.data == "toggle_cover_off":
        job_store.set_setting("cover_enabled", "false")
        await query.answer("⚪ Custom cover feature turned OFF! (Reels will use video thumbnail)", show_alert=False)
        text, keyboard, _ = _build_cover_components()
        try:
            if query.message.photo:
                await query.message.edit_caption(caption=text, reply_markup=keyboard, parse_mode="Markdown")
            else:
                await query.message.edit_text(text=text, reply_markup=keyboard, parse_mode="Markdown")
        except Exception as edit_err:
            logger.debug("Failed to edit cover message inline: %s", edit_err)
    elif query.data == "change_cover":
        context.user_data["awaiting"] = "cover"
        await query.message.reply_text(
            "📸 **Send your new cover image now.**\n\n"
            "Send any photo or image. It will be automatically center-cropped to 9:16 and resized to 1080x1920 for Instagram Reels.",
            parse_mode="Markdown",
        )
    elif query.data == "cookies_refresh":
        msg, keyboard = _build_cookies_dashboard()
        try:
            await query.message.edit_text(msg, reply_markup=keyboard, parse_mode="Markdown")
        except Exception:
            pass
    elif query.data == "cookies_clear_yt":
        target_path = BASE_DIR / "cookies.txt"
        if target_path.is_file():
            content = target_path.read_text(encoding="utf-8", errors="ignore")
            updated = _remove_cookie_domains(content, ("youtube.com", "google.com", "googlevideo.com"))
            if updated.strip():
                target_path.write_text(updated, encoding="utf-8")
            else:
                target_path.unlink(missing_ok=True)
        await query.answer("YouTube cookies cleared!", show_alert=False)
        msg, keyboard = _build_cookies_dashboard()
        try:
            await query.message.edit_text(msg, reply_markup=keyboard, parse_mode="Markdown")
        except Exception:
            pass
    elif query.data == "cookies_clear_ig":
        target_path = BASE_DIR / "cookies.txt"
        if target_path.is_file():
            content = target_path.read_text(encoding="utf-8", errors="ignore")
            updated = _remove_cookie_domains(content, ("instagram.com", "cdninstagram.com"))
            if updated.strip():
                target_path.write_text(updated, encoding="utf-8")
            else:
                target_path.unlink(missing_ok=True)
        await query.answer("Instagram cookies cleared!", show_alert=False)
        msg, keyboard = _build_cookies_dashboard()
        try:
            await query.message.edit_text(msg, reply_markup=keyboard, parse_mode="Markdown")
        except Exception:
            pass
    elif query.data == "cookies_pick_yt":
        context.user_data["awaiting"] = "cookies_yt"
        await query.message.reply_text(
            "🔴 **Update YouTube Cookies**\n\n"
            "Upload your `cookies.txt` file or paste cookie text for YouTube.\n\n"
            "🔒 **Your Instagram cookies are safe** — they will be kept untouched.",
            parse_mode="Markdown",
        )
    elif query.data == "cookies_pick_ig":
        context.user_data["awaiting"] = "cookies_ig"
        await query.message.reply_text(
            "📸 **Update Instagram Cookies**\n\n"
            "Upload your `cookies.txt` file or paste cookie text for Instagram.\n\n"
            "🔒 **Your YouTube cookies are safe** — they will be kept untouched.",
            parse_mode="Markdown",
        )
    elif query.data == "cookies_pick_all":
        context.user_data["awaiting"] = "cookies_all"
        await query.message.reply_text(
            "🔄 **Replace All Cookies**\n\n"
            "Upload a new `cookies.txt` file or paste text to completely overwrite all existing cookies.",
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

        _, keyboard, _ = _build_cover_components()
        enabled = is_cover_enabled()
        feature_note = (
            "✨ Feature is **ON**: This cover will be attached to all future Reels."
            if enabled else
            "💡 Feature is currently **OFF**: Reels will use video's native thumbnail unless you turn it on."
        )

        success_text = (
            f"✅ **New Reels Cover Applied!**\n\n"
            f"• **Resolution**: `{res.width}x{res.height}` (9:16)\n"
            f"• **Format**: `{res.format}` ({res.file_size / 1024:.1f} KB)\n"
            f"• **Optimization**: Center-cropped to 9:16 aspect ratio\n\n"
            f"{feature_note}"
        )

        with open(target_path, "rb") as photo_f:
            await message.reply_photo(
                photo=photo_f,
                caption=success_text,
                reply_markup=keyboard,
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
    awaiting = user_data.get("awaiting") if isinstance(user_data, dict) else None
    awaiting_cookies = awaiting in ("cookies", "cookies_yt", "cookies_ig", "cookies_all")

    # Process if named cookies.txt, ends with .txt, or user selected a cookie option
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
        is_media = any(d in content for d in ["youtube.com", "google.com", "instagram.com"])

        if not (is_netscape or is_media or "\t" in content):
            await status_msg.edit_text(
                "❌ **Invalid Cookies File**\n\n"
                "The file does not appear to be a Netscape-format `cookies.txt`.\n"
                "Please export your cookies using the *Get cookies.txt LOCALLY* extension while on youtube.com or instagram.com."
            )
            return

        target_path = BASE_DIR / "cookies.txt"
        existing_text = target_path.read_text(encoding="utf-8", errors="ignore") if target_path.is_file() else ""
        if awaiting == "cookies_all":
            merged_text = content
            action_desc = "Full Replacement (All Cookies Overwritten)"
        else:
            merged_text = _merge_cookies(existing_text, content)
            if awaiting == "cookies_yt":
                action_desc = "YouTube Updated (Instagram Preserved)"
            elif awaiting == "cookies_ig":
                action_desc = "Instagram Updated (YouTube Preserved)"
            else:
                action_desc = "Smart Merged (Both Preserved)"

        target_path.write_text(merged_text, encoding="utf-8")

        yt_badge, ig_badge = _format_cookies_badge(merged_text)
        size_kb = target_path.stat().st_size / 1024.0

        if isinstance(user_data, dict):
            user_data["awaiting"] = None

        success_msg = (
            f"🍪 **Cookies Updated Successfully!**\n\n"
            f"• **Action**: {action_desc}\n"
            f"• **Destination**: `{target_path.name}`\n"
            f"• **File Size**: `{size_kb:.1f} KB`\n"
            f"• **YouTube Session**: {yt_badge}\n"
            f"• **Instagram Session**: {ig_badge}\n\n"
            f"✨ All future downloads (YouTube & Instagram) will use these cookies immediately."
        )
        await status_msg.edit_text(success_msg, parse_mode="Markdown")
        logger.info("Successfully updated cookies.txt via Telegram (size: %.1f KB, action=%s, YT=%s, IG=%s)", size_kb, action_desc, yt_badge, ig_badge)

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
    cov_mode = "ON (Custom Image)" if is_cover_enabled() else "OFF (Video Thumbnail)"
    cover_status = f"Ready [{cov_mode}]" if cover_res.is_valid else f"Warning: {cover_res.details} [{cov_mode}]"

    # 5. Default Caption Template check
    cap_mode = "ON (Custom Template)" if is_caption_enabled() else "OFF (Video Title)"
    caption_status = f"Ready [{cap_mode}]"

    overall_ok = ig_ok and azure_ok and cover_res.is_valid
    summary = "✅ All systems operational." if overall_ok else "⚠️ One or more checks failed. Review logs."

    report = (
        f"📊 **System Status**\n\n"
        f"• **Telegram Bot**: `{tg_status}`\n"
        f"• **Instagram API**: `{ig_status}`\n"
        f"• **Azure Storage**: `{azure_status}`\n"
        f"• **Cover Image**: `{cover_status}`\n"
        f"• **Default Caption**: `{caption_status}`\n\n"
        f"{summary}"
    )

    if status_msg:
        await status_msg.edit_text(report, parse_mode="Markdown")
    elif update.effective_message:
        await update.effective_message.reply_text(report, parse_mode="Markdown")


# =====================================================================
# Stage 2: YouTube URL Detection & Download Pipeline
# =====================================================================

def _clean_title(title: Optional[str]) -> str:
    """Sanitizes titles so markdown control characters (*, _, `, [, ]) never break Telegram formatting."""
    if not title:
        return "Instagram Reel"
    # Replace underscores with spaces and remove markdown delimiters
    clean = str(title).replace("_", " ").replace("*", "").replace("`", "").replace("[", "(").replace("]", ")")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:80] or "Instagram Reel"


def resolve_job_caption(result: Any, job_id: str) -> str:
    """
    Determines the final caption for a post:
    1. If an explicit caption was assigned to this job, use it.
    2. If custom caption feature is ON (/caption or /title), use default template.
    3. If custom caption is OFF:
       - Check if original post has a valid caption/title (not empty, and not generic).
       - If valid, use original post caption/title.
       - If NO caption/title exists (or is generic/empty), fallback to default template from /caption or /title!
    """
    job = job_store.get_job(job_id)
    if job and job.get("caption"):
        return job["caption"].strip()

    default_template = (job_store.get_setting("default_caption") or DEFAULT_MEME_CAPTION).strip()

    if is_caption_enabled():
        logger.info("[%s] Custom caption is ON. Using custom template.", job_id)
        return default_template

    # Custom caption is OFF: inspect original post caption / title
    clean_title = _clean_title(result.title) if hasattr(result, "title") else ""
    raw_caption = getattr(result, "caption", None)
    original_caption = (raw_caption or clean_title or "").strip()

    GENERIC_NAMES = {
        "instagram media",
        "instagram video",
        "instagram photo",
        "instagram reel",
        "instagram post",
        "video by",
        "post by",
        "untitled",
        "none",
        "",
    }
    is_generic = (
        not original_caption
        or original_caption.lower() in GENERIC_NAMES
        or original_caption.lower().startswith("video by ")
        or original_caption.lower().startswith("post by ")
    )

    if not is_generic:
        logger.info("[%s] Custom caption is OFF. Using original post caption (%d chars): %s", job_id, len(original_caption), original_caption[:60])
        return original_caption

    # No caption or title was provided in the Instagram post -> fallback to default template from /caption or /title
    logger.info("[%s] Original post has no caption/title. Falling back to default template from /caption.", job_id)
    return default_template



async def _update_progress_message(
    status_msg: Message,
    text: str,
) -> None:
    """Safely edits the status message with automatic fallback to plain text if Markdown parsing fails."""
    try:
        await status_msg.edit_text(text, parse_mode="Markdown")
    except Exception as exc:
        err_str = str(exc)
        if "Message is not modified" in err_str:
            return
        # If Telegram rejected Markdown (e.g. Can't parse entities due to special characters):
        try:
            plain = (
                text.replace("**", "")
                .replace("`", "")
                .replace("*", "")
                .replace("_", " ")
            )
            await status_msg.edit_text(plain, parse_mode=None)
        except Exception as fallback_exc:
            if "Message is not modified" not in str(fallback_exc):
                logger.debug("Minor exception editing progress message: %s", fallback_exc)


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
        clean_title = _clean_title(result.title)
        logger.info("[%s] Download succeeded: '%s' (%s). Starting processing...", job_id, result.title, size_str)

        # Set job caption (uses original post caption/title, or falls back to /caption default if none exists)
        final_caption = resolve_job_caption(result, job_id)
        job_store.set_job_caption(job_id, final_caption)

        # Branch for multi-slide carousel posts (2 or more slides)
        if result.is_carousel and result.carousel_items and len(result.carousel_items) > 1:
            await _handle_carousel_pipeline(
                job_id=job_id,
                result=result,
                status_msg=status_msg,
                loop=loop,
            )
            return

        # Branch for single photo / image post
        is_single_image = getattr(result, "is_image", False) or Path(result.file_path).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        if is_single_image:
            if result.audio_file and Path(result.audio_file).is_file():
                logger.info("[%s] Single image post has attached audio track! Converting to Reel with audio...", job_id)
                await _handle_single_image_with_audio_pipeline(
                    job_id=job_id,
                    result=result,
                    status_msg=status_msg,
                    loop=loop,
                )
                return
            else:
                await _handle_single_image_pipeline(
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
                f"🎬 **{clean_title}**\n\n"
                f"Position: `#{position}` in queue (Rendering 1 at a time to prevent server overload)"
            )
            await _update_progress_message(status_msg, wait_text)

        proc_start_msg = (
            f"⚡ **Processing Video...**\n\n"
            f"🎬 **{clean_title}**\n\n"
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
                    f"🎬 **{clean_title}**\n\n"
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
            f"🎬 **{clean_title}**\n\n"
            f"☁️ Uploading to cloud storage..."
        )
        await _update_progress_message(status_msg, upload_msg)

        try:
            # 1. Ensure cover image is hosted if custom cover feature is on
            if is_cover_enabled():
                await azure_storage_manager.ensure_cover_uploaded(config.default_cover_path)

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
                    f"🎬 **{clean_title}**\n\n"
                    f"🚀 Publishing to Instagram...",
                )
                asyncio.create_task(_run_instagram_publish_background(job_id, existing_caption, status_msg))
            else:
                # Bare link without caption -> prompt user in Telegram
                job_store.set_awaiting_caption(job_id)
                prompt_msg = (
                    f"🎬 **Video Ready!**\n\n"
                    f"**{clean_title}**\n\n"
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

    cover_sas_url = None
    if is_cover_enabled():
        cover_data = await azure_storage_manager.ensure_cover_uploaded(config.default_cover_path)
        cover_sas_url = cover_data.get("sas_url")
        logger.info("[%s] Custom cover feature is ON. Using custom cover: %s", job_id, cover_sas_url)
    else:
        logger.info("[%s] Custom cover feature is OFF. Publishing Reel using video native thumbnail.", job_id)

    last_update_time = 0.0

    async def publish_progress(status_code: str, elapsed: float) -> None:
        nonlocal last_update_time
        now = time.monotonic()
        if now - last_update_time >= 3.0:
            last_update_time = now
            title = _clean_title(job.get("title"))
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

        title = _clean_title(job.get("title"))
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
    clean_title = _clean_title(result.title)
    logger.info("[%s] Beginning carousel pipeline with %d slides...", job_id, total)

    try:
        await _update_progress_message(
            status_msg,
            f"⚡ **Processing Carousel Post...**\n\n"
            f"📸 **{clean_title}**\n\n"
            f"Found {total} slides. Preparing media for Instagram...",
        )

        uploaded_items: List[Dict[str, str]] = []
        target_aspect_ratio: Optional[float] = None

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

                if target_aspect_ratio is None:
                    try:
                        vinfo = get_video_info(processed_slide_path)
                        vw = vinfo.get("width") or 1080
                        vh = vinfo.get("height") or 1080
                        target_aspect_ratio = max(0.8, min(1.91, vw / vh))
                    except Exception:
                        target_aspect_ratio = 1.0

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
                        if target_aspect_ratio is None:
                            # Slide 1 sets the target aspect ratio for all slides (Instagram allows 4:5 to 1.91:1)
                            raw_ratio = rgb_img.width / rgb_img.height
                            target_aspect_ratio = max(0.8, min(1.91, raw_ratio))

                        # Normalize slide aspect ratio to match target_aspect_ratio to prevent Meta error 2207050
                        curr_ratio = rgb_img.width / rgb_img.height
                        if abs(curr_ratio - target_aspect_ratio) > 0.02:
                            if curr_ratio > target_aspect_ratio:
                                # Wider than target: pad height (top & bottom)
                                new_height = int(round(rgb_img.width / target_aspect_ratio))
                                if new_height % 2 != 0:
                                    new_height += 1
                                canvas = Image.new("RGB", (rgb_img.width, new_height), (0, 0, 0))
                                canvas.paste(rgb_img, (0, (new_height - rgb_img.height) // 2))
                                rgb_img = canvas
                            else:
                                # Taller than target: pad width (left & right)
                                new_width = int(round(rgb_img.height * target_aspect_ratio))
                                if new_width % 2 != 0:
                                    new_width += 1
                                canvas = Image.new("RGB", (new_width, rgb_img.height), (0, 0, 0))
                                canvas.paste(rgb_img, ((new_width - rgb_img.width) // 2, 0))
                                rgb_img = canvas

                        # Ensure both width and height are even for video encoding compatibility
                        if rgb_img.width % 2 != 0 or rgb_img.height % 2 != 0:
                            even_w = rgb_img.width if rgb_img.width % 2 == 0 else rgb_img.width - 1
                            even_h = rgb_img.height if rgb_img.height % 2 == 0 else rgb_img.height - 1
                            rgb_img = rgb_img.crop((0, 0, even_w, even_h))

                        rgb_img.save(processed_img_path, format="JPEG", quality=95, optimize=True)
                except Exception as img_err:
                    logger.warning("[%s] Pillow processing failed for slide %d: %s. Copying original...", job_id, idx, img_err)
                    shutil.copy2(source_path, processed_img_path)

                # If post has attached audio, bake it into a video slide so sound plays on Instagram!
                if result.audio_file and Path(result.audio_file).is_file():
                    await _update_progress_message(
                        status_msg,
                        f"⚡ **Processing Carousel ({idx}/{total})...**\n\n"
                        f"🎵 Adding background audio to slide {idx}...",
                    )
                    processed_slide_path = PROCESSED_DIR / f"{job_id}_slide_{idx}.mp4"
                    cmd = [
                        "ffmpeg", "-y",
                        "-loop", "1",
                        "-i", str(processed_img_path),
                        "-i", str(result.audio_file),
                        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                        "-c:v", "libx264",
                        "-preset", "veryfast",
                        "-crf", "17",
                        "-tune", "stillimage",
                        "-c:a", "aac",
                        "-b:a", "320k",
                        "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart",
                        "-t", "7.0",
                        "-shortest",
                        str(processed_slide_path),
                    ]
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()

                    await _update_progress_message(
                        status_msg,
                        f"☁️ **Uploading Carousel ({idx}/{total})...**\n\n"
                        f"Uploading video slide {idx} with audio...",
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
        caption = (job.get("caption") if job else None) or resolve_job_caption(result, job_id)
        job_store.set_job_caption(job_id, caption)

        await _update_progress_message(
            status_msg,
            f"⚡ **Publishing Carousel...**\n\n"
            f"📸 **{clean_title}** ({total} slides)\n\n"
            f"🚀 Publishing to Instagram...",
        )
        asyncio.create_task(
            _run_instagram_carousel_publish_background(
                job_id=job_id,
                items=uploaded_items,
                caption=caption,
                status_msg=status_msg,
                title=clean_title,
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
    clean_car_title = _clean_title(title)
    last_update_time = 0.0

    async def publish_progress(status_code: str, elapsed: float) -> None:
        nonlocal last_update_time
        now = time.monotonic()
        if now - last_update_time >= 2.5:
            last_update_time = now
            if isinstance(status_code, str) and (" " in status_code or not status_code.isupper()):
                stage_desc = status_code
            else:
                stage_desc = f"Rendering on Instagram ({int(elapsed)}s)..."
            msg_text = (
                f"⚡ **Publishing Carousel...**\n\n"
                f"📸 **{clean_car_title}** ({len(items)} slides)\n\n"
                f"🚀 {stage_desc}"
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
            f"📸 **{clean_car_title}** ({len(items)} slides)\n\n"
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


async def _handle_single_image_with_audio_pipeline(
    job_id: str,
    result: DownloadResult,
    status_msg: Message,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """
    Handles single Instagram photo/image posts that have an attached audio track.
    Renders the image and audio into a full video Reel so the audio plays natively on Instagram!
    """
    clean_title = _clean_title(result.title)
    source_path = Path(result.file_path)
    audio_path = Path(result.audio_file)
    logger.info("[%s] Beginning single photo with audio pipeline: img=%s, audio=%s", job_id, source_path.name, audio_path.name)

    try:
        await _update_progress_message(
            status_msg,
            f"⚡ **Processing Video Reel...**\n\n"
            f"🎬 **{clean_title}**\n\n"
            f"🎵 Baking attached audio into Reel...",
        )

        processed_img_path = PROCESSED_DIR / f"{job_id}_frame.jpg"
        from PIL import Image

        with Image.open(source_path) as img:
            rgb_img = img.convert("RGB")
            # Instagram allows aspect ratio between 4:5 (0.80) and 1.91:1
            curr_ratio = rgb_img.width / rgb_img.height
            if curr_ratio < 0.8:
                new_width = int(round(rgb_img.height * 0.8))
                if new_width % 2 != 0:
                    new_width += 1
                canvas = Image.new("RGB", (new_width, rgb_img.height), (0, 0, 0))
                canvas.paste(rgb_img, ((new_width - rgb_img.width) // 2, 0))
                rgb_img = canvas
            elif curr_ratio > 1.91:
                new_height = int(round(rgb_img.width / 1.91))
                if new_height % 2 != 0:
                    new_height += 1
                canvas = Image.new("RGB", (rgb_img.width, new_height), (0, 0, 0))
                canvas.paste(rgb_img, (0, (new_height - rgb_img.height) // 2))
                rgb_img = canvas

            # Ensure even dimensions for video encoding
            if rgb_img.width % 2 != 0 or rgb_img.height % 2 != 0:
                even_w = rgb_img.width if rgb_img.width % 2 == 0 else rgb_img.width - 1
                even_h = rgb_img.height if rgb_img.height % 2 == 0 else rgb_img.height - 1
                rgb_img = rgb_img.crop((0, 0, even_w, even_h))

            rgb_img.save(processed_img_path, format="JPEG", quality=95, optimize=True)

        rendered_video_path = PROCESSED_DIR / f"{job_id}.mp4"

        # Determine audio duration using ffprobe
        audio_duration = 15.0
        try:
            from bot.processor import get_video_info
            ainfo = get_video_info(audio_path)
            if ainfo.get("duration"):
                audio_duration = float(ainfo["duration"])
        except Exception:
            pass
        audio_duration = min(60.0, max(5.0, audio_duration))

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", str(processed_img_path),
            "-i", str(audio_path),
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "17",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-b:a", "320k",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-t", str(audio_duration),
            "-shortest",
            str(rendered_video_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        # Upload rendered Reel to Azure Blob Storage
        await _update_progress_message(
            status_msg,
            f"☁️ **Uploading Reel...**\n\n"
            f"🎬 **{clean_title}**\n\n"
            f"Uploading to cloud storage...",
        )
        upload_data = await azure_storage_manager.upload_video_blob(
            job_id=job_id,
            file_path=str(rendered_video_path),
        )
        job_store.complete_azure_upload(
            job_id=job_id,
            blob_name=upload_data["blob_name"],
            sas_url=upload_data["sas_url"],
            expires_at=upload_data["expires_at"],
        )

        job = job_store.get_job(job_id)
        caption = (job.get("caption") if job else None) or resolve_job_caption(result, job_id)
        job_store.set_job_caption(job_id, caption)

        await _update_progress_message(
            status_msg,
            f"⚡ **Publishing Reel...**\n\n"
            f"🎬 **{clean_title}**\n\n"
            f"🚀 Publishing to Instagram...",
        )
        asyncio.create_task(
            _run_instagram_publish_background(
                job_id=job_id,
                caption=caption,
                status_msg=status_msg,
            )
        )

    except Exception as exc:
        err_msg = str(exc)
        logger.error("[%s] Single image with audio pipeline failed: %s", job_id, err_msg, exc_info=True)
        job_store.fail_processing(job_id, err_msg)
        storage_cleaner.cleanup_job_local_files(job_id)
        await _update_progress_message(
            status_msg,
            f"❌ **Reel Processing Failed**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Reason**: {err_msg[:200]}\n\n"
            f"Use `/retry {job_id}` to retry.",
        )


async def _handle_single_image_pipeline(
    job_id: str,
    result: DownloadResult,
    status_msg: Message,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Handles single Instagram photo/image posts (e.g. meme, single slide)."""
    clean_title = _clean_title(result.title)
    source_path = Path(result.file_path)
    logger.info("[%s] Beginning single photo pipeline: %s", job_id, source_path.name)

    try:
        await _update_progress_message(
            status_msg,
            f"⚡ **Processing Photo Post...**\n\n"
            f"🖼️ **{clean_title}**\n\n"
            f"Optimizing image for Instagram specifications...",
        )

        processed_img_path = PROCESSED_DIR / f"{job_id}.jpg"
        from PIL import Image

        with Image.open(source_path) as img:
            rgb_img = img.convert("RGB")
            # Instagram requires aspect ratio between 4:5 (0.80) and 1.91:1
            ratio = rgb_img.width / rgb_img.height
            if ratio < 0.8:
                new_width = int(round(rgb_img.height * 0.8))
                canvas = Image.new("RGB", (new_width, rgb_img.height), (0, 0, 0))
                canvas.paste(rgb_img, ((new_width - rgb_img.width) // 2, 0))
                canvas.save(processed_img_path, format="JPEG", quality=95, optimize=True)
            elif ratio > 1.91:
                new_height = int(round(rgb_img.width / 1.91))
                canvas = Image.new("RGB", (rgb_img.width, new_height), (0, 0, 0))
                canvas.paste(rgb_img, (0, (new_height - rgb_img.height) // 2))
                canvas.save(processed_img_path, format="JPEG", quality=95, optimize=True)
            else:
                rgb_img.save(processed_img_path, format="JPEG", quality=95, optimize=True)

        # Upload image to Azure Blob Storage
        await _update_progress_message(
            status_msg,
            f"☁️ **Uploading Photo...**\n\n"
            f"🖼️ **{clean_title}**\n\n"
            f"Uploading to cloud storage...",
        )
        upload_data = await azure_storage_manager.upload_media_blob(
            job_id=job_id,
            file_path=str(processed_img_path),
            blob_name=f"{job_id}.jpg",
            content_type="image/jpeg",
        )
        image_url = upload_data["sas_url"]

        job = job_store.get_job(job_id)
        caption = (job.get("caption") if job else None) or resolve_job_caption(result, job_id)
        job_store.set_job_caption(job_id, caption)

        await _update_progress_message(
            status_msg,
            f"⚡ **Publishing Photo...**\n\n"
            f"🖼️ **{clean_title}**\n\n"
            f"🚀 Publishing to Instagram...",
        )
        asyncio.create_task(
            _run_instagram_photo_publish_background(
                job_id=job_id,
                image_url=image_url,
                caption=caption,
                status_msg=status_msg,
                title=clean_title,
            )
        )

    except asyncio.CancelledError:
        logger.info("[%s] Photo pipeline was cancelled.", job_id)
        storage_cleaner.cleanup_job_local_files(job_id)
        raise
    except Exception as exc:
        err_msg = str(exc)
        job_store.fail_processing(job_id, err_msg)
        storage_cleaner.cleanup_job_local_files(job_id)
        logger.error("[%s] Photo processing pipeline failed: %s", job_id, err_msg, exc_info=True)
        await _update_progress_message(
            status_msg,
            f"❌ **Photo Processing Failed**\n\n"
            f"• **Job ID**: `{job_id}`\n"
            f"• **Reason**: {err_msg[:200]}\n\n"
            f"Review logs or use `/retry {job_id}`.",
        )


async def _run_instagram_photo_publish_background(
    job_id: str,
    image_url: str,
    caption: str,
    status_msg: Message,
    title: str = "Instagram Photo",
) -> None:
    """Executes the single photo Instagram publishing pipeline."""
    current_task = asyncio.current_task()
    if current_task:
        task_registry.register_task(job_id, current_task)

    job_store.start_publishing(job_id, caption=caption)
    clean_photo_title = _clean_title(title)
    last_update_time = 0.0

    async def publish_progress(status_code: str, elapsed: float) -> None:
        nonlocal last_update_time
        now = time.monotonic()
        if now - last_update_time >= 3.0:
            last_update_time = now
            msg_text = (
                f"⚡ **Publishing Photo...**\n\n"
                f"🖼️ **{clean_photo_title}**\n\n"
                f"🚀 Rendering on Instagram ({int(elapsed)}s)..."
            )
            await _update_progress_message(status_msg, msg_text)

    try:
        publish_res = await instagram_publisher.publish_single_photo(
            job_id=job_id,
            image_url=image_url,
            caption=caption,
            progress_callback=publish_progress,
        )

        storage_cleaner.cleanup_job_local_files(job_id)
        failure_tracker.record_success("instagram_publish")

        permalink = publish_res.get("permalink", "https://instagram.com")
        success_msg = (
            f"🎉 **Photo Published!**\n\n"
            f"🖼️ **{clean_photo_title}**\n\n"
            f"👉 [Watch on Instagram]({permalink})"
        )
        await _update_progress_message(status_msg, success_msg)
        logger.info("[%s] Instagram Photo successfully published: %s", job_id, permalink)

    except asyncio.CancelledError:
        logger.info("[%s] Instagram photo publish task was cancelled.", job_id)
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
            f"⚠️ **Action Required**: Please update your permanent access token."
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
        logger.error("[%s] Instagram photo publishing failed: %s", job_id, err_msg, exc_info=True)
        fail_msg = (
            f"❌ **Instagram Photo Publish Failed**\n\n"
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
        text, keyboard = _build_caption_components()
        await message.reply_text(
            f"✅ **Default caption updated successfully!**\n\n{text}",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return

    # 2. Check if user is sending pasted cookies text (prompted or unprompted)
    raw_input = message.text.strip()
    awaiting = user_data.get("awaiting") if isinstance(user_data, dict) else None
    is_cookie_text = (
        (awaiting in ("cookies", "cookies_yt", "cookies_ig", "cookies_all"))
        or raw_input.startswith(("# Netscape", "# HTTP Cookie File"))
        or (".youtube.com\t" in raw_input)
        or (".instagram.com\t" in raw_input)
    )
    if is_cookie_text:
        if not any(d in raw_input for d in ["youtube.com", "google.com", "instagram.com", "# Netscape", "\t"]):
            await message.reply_text(
                "❌ **Invalid Cookies Text**\n\n"
                "The text does not appear to contain Netscape-format YouTube or Instagram cookies.\n"
                "Please make sure it has tab-separated cookie entries.",
                parse_mode="Markdown",
            )
            return

        target_path = BASE_DIR / "cookies.txt"
        existing_text = target_path.read_text(encoding="utf-8", errors="ignore") if target_path.is_file() else ""
        if awaiting == "cookies_all":
            merged_text = raw_input
            action_desc = "Full Replacement (All Cookies Overwritten)"
        else:
            merged_text = _merge_cookies(existing_text, raw_input)
            if awaiting == "cookies_yt":
                action_desc = "YouTube Updated (Instagram Preserved)"
            elif awaiting == "cookies_ig":
                action_desc = "Instagram Updated (YouTube Preserved)"
            else:
                action_desc = "Smart Merged (Both Preserved)"

        target_path.write_text(merged_text, encoding="utf-8")

        yt_badge, ig_badge = _format_cookies_badge(merged_text)
        size_kb = target_path.stat().st_size / 1024.0

        if isinstance(user_data, dict):
            user_data["awaiting"] = None

        await message.reply_text(
            f"🍪 **Cookies Updated Successfully!**\n\n"
            f"• **Action**: {action_desc}\n"
            f"• **Destination**: `{target_path.name}`\n"
            f"• **File Size**: `{size_kb:.1f} KB`\n"
            f"• **YouTube Session**: {yt_badge}\n"
            f"• **Instagram Session**: {ig_badge}\n\n"
            f"✨ All future downloads (YouTube & Instagram) will use these cookies immediately.",
            parse_mode="Markdown",
        )
        logger.info("Successfully updated cookies.txt via pasted text (size: %.1f KB, action=%s, YT=%s, IG=%s)", size_kb, action_desc, yt_badge, ig_badge)
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

    # Determine caption:
    # 1. Inline caption typed directly in the Telegram message -> Always use it.
    # 2. Custom caption template is turned ON -> Use default template from /caption.
    # 3. Custom caption template is turned OFF (Default) -> Leave None for now;
    #    the original post caption/title will be assigned automatically after download!
    if inline_caption:
        final_caption = inline_caption
    elif is_caption_enabled():
        final_caption = job_store.get_setting("default_caption") or DEFAULT_MEME_CAPTION
    else:
        final_caption = None

    # 1. Generate unique Job ID (UUID4)
    job_id = str(uuid.uuid4())
    logger.info(
        "New download job registered: job_id=%s, user_id=%s, url=%s, platform=%s, has_inline_caption=%s, caption_template_on=%s",
        job_id,
        user_id,
        url,
        platform_name,
        bool(inline_caption),
        is_caption_enabled(),
    )

    # 2. Persist in SQLite Job Store (with final caption stored if pre-determined)
    job_store.create_job(job_id=job_id, user_id=user_id, source_url=url, caption=final_caption)

    # 3. Send immediate acknowledgment message
    caption_note = " (with custom caption)" if inline_caption else " (with template)" if is_caption_enabled() else ""
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
        if is_cover_enabled():
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
    Uses Unshockabl VideoProcessor implemented in Stage 3.
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
    cover_sas_url = None
    if is_cover_enabled():
        cover_data = await azure_storage_manager.ensure_cover_uploaded(config.default_cover_path)
        cover_sas_url = cover_data.get("sas_url")
    return await instagram_publisher.publish_reel(
        job_id=job_id,
        video_url=video_url,
        caption=caption,
        cover_url=cover_sas_url,
    )

