"""Main entry point for the Telegram bot."""

import os
import sys
from pathlib import Path

# Ensure project root is in sys.path when running as a standalone script
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

# 1. Initialize logging
from bot.logger import setup_logging
# 2. Load and validate configuration
from bot.config import config
# 3. Database job store
from bot.db import job_store
# 4. Import command handlers, message handlers, and error handler
from bot.handlers import (
    callback_query_handler,
    cancel_command,
    caption_command,
    cookies_command,
    cover_command,
    document_upload_handler,
    global_error_handler,
    job_detail_command,
    jobs_command,
    photo_upload_handler,
    retry_command,
    start_command,
    status_command,
    youtube_url_handler,
)
from bot.recovery import startup_recovery, storage_cleaner


def main() -> None:
    """Initializes and runs the Telegram bot in polling mode."""
    # Setup structured logger with configured log level
    logger = setup_logging(config.log_level)
    logger.info("Initializing bot with %d authorized user ID(s)...", len(config.allowed_telegram_user_ids))

    # Stage 7: Startup recovery scan for interrupted non-terminal jobs
    recovered = startup_recovery.scan_and_recover_jobs()
    recovery_report = startup_recovery.format_recovery_report(recovered) if recovered else None
    if recovered:
        logger.warning(
            "Recovered and handled %d interrupted job(s) from previous session.",
            len(recovered),
        )

    # Safety net retention sweep (clean unlinked files older than 48h)
    swept_files = storage_cleaner.periodic_storage_sweep(48)
    if swept_files:
        logger.info("Periodic storage safety net sweep removed %d stale file(s).", len(swept_files))

    # Inspect default cover image for Instagram Reels compliance
    from bot.cover import inspect_cover_image
    cover_res = inspect_cover_image(config.default_cover_path)
    if cover_res.is_valid:
        logger.info("Default cover image loaded: %s [%s]", config.default_cover_path.name, cover_res.details)
    else:
        logger.warning(
            "Default cover image warning [%s]: %s (%s)",
            config.default_cover_path.name,
            cover_res.status_summary,
            cover_res.details,
        )

    # Ensure dedicated Azure Blob Storage container exists & sweep expired blobs
    from bot.azure_storage import azure_storage_manager
    try:
        azure_storage_manager.ensure_container_exists()
        cleaned = azure_storage_manager.cleanup_expired_video_blobs()
        if cleaned:
            logger.info("Startup lifecycle sweep cleaned %d expired video blob(s): %s", len(cleaned), cleaned)
    except Exception as exc:
        logger.warning("Azure container startup initialization check encountered: %s", exc)

    async def post_init(app) -> None:
        """Configures Telegram Bot commands, descriptions, and sends startup recovery notices."""
        from telegram import BotCommand

        # 1. Register menu commands so the [/] Menu button appears beside the typing box
        commands = [
            BotCommand("start", "Welcome & instructions"),
            BotCommand("caption", "View or change default caption"),
            BotCommand("cover", "View or change Reels cover image"),
            BotCommand("cookies", "View or update YouTube cookies.txt"),
            BotCommand("status", "System & API health check"),
            BotCommand("jobs", "List active jobs"),
            BotCommand("cancel", "Cancel an active job or prompt"),
            BotCommand("retry", "Retry a failed job"),
        ]
        try:
            await app.bot.set_my_commands(commands)
            logger.info("Registered %d Telegram bot commands.", len(commands))
        except Exception as exc:
            logger.warning("Failed to register bot commands: %s", exc)

        # 2. Set bot description
        try:
            await app.bot.set_my_description(
                "🎬 YouTube to Instagram Reels Publisher\n\n"
                "Send any YouTube Shorts or video link to automatically download, optimize, and publish directly to Instagram Reels."
            )
            await app.bot.set_my_short_description(
                "Automated YouTube to Instagram Reels Publisher."
            )
        except Exception as exc:
            logger.debug("Non-fatal note updating bot description: %s", exc)

        # 3. Send startup recovery notifications if any jobs were recovered
        if recovery_report:
            for uid in config.allowed_telegram_user_ids:
                try:
                    await app.bot.send_message(chat_id=uid, text=recovery_report, parse_mode="Markdown")
                except Exception as exc:
                    logger.debug("Failed to send startup recovery notice to user %s: %s", uid, exc)

    # Build python-telegram-bot Application
    application = (
        ApplicationBuilder()
        .token(config.telegram_bot_token)
        .post_init(post_init)
        .build()
    )

    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("caption", caption_command))
    application.add_handler(CommandHandler("cover", cover_command))
    application.add_handler(CommandHandler("cookies", cookies_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("jobs", jobs_command))
    application.add_handler(CommandHandler("job", job_detail_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("retry", retry_command))

    # Register callback query handler for interactive buttons
    application.add_handler(CallbackQueryHandler(callback_query_handler))

    # Register photo / image document handler for cover image uploads
    application.add_handler(
        MessageHandler(filters.PHOTO | (filters.Document.IMAGE & (~filters.COMMAND)), photo_upload_handler)
    )

    # Register document upload handler for cookies.txt
    application.add_handler(
        MessageHandler(filters.Document.ALL & (~filters.COMMAND), document_upload_handler)
    )

    # Register YouTube URL text message handler (filters out commands)
    application.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), youtube_url_handler)
    )

    # Register global error handler
    application.add_error_handler(global_error_handler)

    logger.info("Starting Telegram bot polling (drop_pending_updates=True)...")
    try:
        application.run_polling(drop_pending_updates=True)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by system signal or user interruption.")
    except Exception as exc:
        logger.critical("Bot terminated unexpectedly: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
