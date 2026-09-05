"""Main entry point for the Telegram bot."""

import os
import sys
from pathlib import Path

# Ensure project root is in sys.path when running as a standalone script
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from telegram.ext import ApplicationBuilder, CommandHandler

# 1. Initialize logging
from bot.logger import setup_logging
# 2. Load and validate configuration
from bot.config import config
# 3. Import command handlers and error handler
from bot.handlers import (
    global_error_handler,
    start_command,
    status_command,
)


def main() -> None:
    """Initializes and runs the Telegram bot in polling mode."""
    # Setup structured logger with configured log level
    logger = setup_logging(config.log_level)
    logger.info("Initializing bot with %d authorized user ID(s)...", len(config.allowed_telegram_user_ids))

    # Build python-telegram-bot Application
    application = (
        ApplicationBuilder()
        .token(config.telegram_bot_token)
        .build()
    )

    # Register handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))

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
