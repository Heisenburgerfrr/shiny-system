"""Telegram command handlers, access control, and diagnostic checks."""

import asyncio
import functools
import logging
from typing import Callable, Tuple

import httpx
from azure.storage.blob import BlobServiceClient
from telegram import Update
from telegram.ext import ContextTypes

from bot.config import config

logger = logging.getLogger("bot.handlers")


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
        "Available commands:\n"
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

    overall_ok = ig_ok and azure_ok
    summary = "✅ All systems operational." if overall_ok else "⚠️ One or more checks failed. Review logs."

    report = (
        f"📊 **System Status Report**\n\n"
        f"• **Telegram Bot API**: `{tg_status}`\n"
        f"• **Instagram Graph API**: `{ig_status}`\n"
        f"• **Azure Blob Storage**: `{azure_status}`\n\n"
        f"{summary}"
    )

    if status_msg:
        await status_msg.edit_text(report, parse_mode="Markdown")
    elif update.effective_message:
        await update.effective_message.reply_text(report, parse_mode="Markdown")


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

async def download_youtube_video(url: str) -> str:
    """
    Placeholder for downloading YouTube videos.
    # TODO: Stage 2 - Implement yt-dlp download pipeline
    """
    raise NotImplementedError("Stage 2 not implemented yet.")


async def process_video(input_path: str) -> str:
    """
    Placeholder for video processing and aspect ratio conversion.
    # TODO: Stage 3 - Implement ffmpeg video processing pipeline
    """
    raise NotImplementedError("Stage 3 not implemented yet.")


async def publish_to_instagram(video_url: str, caption: str) -> str:
    """
    Placeholder for uploading to Azure Blob and publishing to Instagram.
    # TODO: Stage 4 - Implement Azure Blob upload and Instagram Graph API publishing
    """
    raise NotImplementedError("Stage 4 not implemented yet.")
