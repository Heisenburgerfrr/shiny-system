"""Configuration loader and environment validator."""

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set
from dotenv import load_dotenv

# Load variables from .env file if present
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """Immutable application settings validated at startup."""
    telegram_bot_token: str
    allowed_telegram_user_ids: Set[int]
    instagram_access_token: str
    instagram_business_account_id: str
    azure_storage_connection_string: str
    log_level: str
    ytdlp_cookies_path: Optional[str]
    ytdlp_proxy_url: Optional[str]
    ytdlp_player_clients: List[str]
    default_cover_path: Path


def _load_and_validate_settings() -> Settings:
    """
    Reads required environment variables and validates them.
    Fails fast with a comprehensive error message listing all missing configurations.
    """
    errors = []

    # 1. TELEGRAM_BOT_TOKEN
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not telegram_bot_token:
        errors.append("Missing required environment variable: TELEGRAM_BOT_TOKEN")

    # 2. ALLOWED_TELEGRAM_USER_IDS (supports ALLOWED_TELEGRAM_USERS fallback)
    raw_user_ids = os.getenv("ALLOWED_TELEGRAM_USER_IDS") or os.getenv("ALLOWED_TELEGRAM_USERS") or ""
    allowed_telegram_user_ids: Set[int] = set()

    if not raw_user_ids.strip():
        errors.append(
            "Missing required environment variable: ALLOWED_TELEGRAM_USER_IDS "
            "(comma-separated list of numeric Telegram user IDs)"
        )
    else:
        for part in raw_user_ids.split(","):
            part_clean = part.strip()
            if not part_clean:
                continue
            try:
                allowed_telegram_user_ids.add(int(part_clean))
            except ValueError:
                errors.append(
                    f"Invalid user ID in ALLOWED_TELEGRAM_USER_IDS: '{part_clean}' is not a valid integer."
                )

        if not allowed_telegram_user_ids and not errors:
            errors.append("ALLOWED_TELEGRAM_USER_IDS must contain at least one valid numeric Telegram user ID.")

    # 3. INSTAGRAM_ACCESS_TOKEN
    instagram_access_token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
    if not instagram_access_token:
        errors.append("Missing required environment variable: INSTAGRAM_ACCESS_TOKEN")

    # 4. INSTAGRAM_BUSINESS_ACCOUNT_ID (supports INSTAGRAM_ACCOUNT_ID fallback)
    instagram_business_account_id = (
        os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID") or os.getenv("INSTAGRAM_ACCOUNT_ID") or ""
    ).strip()
    if not instagram_business_account_id:
        errors.append("Missing required environment variable: INSTAGRAM_BUSINESS_ACCOUNT_ID")

    # 5. AZURE_STORAGE_CONNECTION_STRING
    azure_storage_connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not azure_storage_connection_string:
        errors.append("Missing required environment variable: AZURE_STORAGE_CONNECTION_STRING")

    # 6. LOG_LEVEL (optional, default INFO)
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    valid_log_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if log_level not in valid_log_levels:
        errors.append(
            f"Invalid LOG_LEVEL '{log_level}'. Must be one of: {', '.join(sorted(valid_log_levels))}"
        )

    # 7. YTDLP_COOKIES_PATH (optional)
    ytdlp_cookies_path = os.getenv("YTDLP_COOKIES_PATH", "").strip() or None

    # 8. YTDLP_PROXY_URL (optional)
    ytdlp_proxy_url = os.getenv("YTDLP_PROXY_URL", "").strip() or None

    # 9. YTDLP_PLAYER_CLIENTS (optional, default: android,ios,web)
    raw_clients = os.getenv("YTDLP_PLAYER_CLIENTS", "android,ios,web").strip()
    ytdlp_player_clients = [c.strip() for c in raw_clients.split(",") if c.strip()]
    if not ytdlp_player_clients:
        ytdlp_player_clients = ["android", "ios", "web"]

    # 10. DEFAULT_COVER_PATH (Stage 4: Fixed Default Cover Image)
    custom_cover = os.getenv("DEFAULT_COVER_PATH", "").strip()
    if custom_cover:
        default_cover_path = Path(custom_cover)
    else:
        # Check standard locations in project root
        if (BASE_DIR / "cover.png").exists():
            default_cover_path = BASE_DIR / "cover.png"
        elif (BASE_DIR / "cover.jpg").exists():
            default_cover_path = BASE_DIR / "cover.jpg"
        else:
            default_cover_path = BASE_DIR / "cover.png"

    if not default_cover_path.is_file():
        errors.append(
            f"Missing required default cover image: '{default_cover_path}' was not found. "
            f"Please place cover.png or cover.jpg in the project root."
        )
    else:
        # Validate that the image file can actually be opened
        try:
            from PIL import Image
            with Image.open(default_cover_path) as img:
                img.verify()
        except Exception as exc:
            errors.append(f"Default cover image '{default_cover_path}' is corrupted or unreadable: {exc}")

    if errors:
        error_msg = "\n".join(f"  - {err}" for err in errors)
        raise RuntimeError(
            f"\n[CRITICAL] Configuration validation failed at startup:\n{error_msg}\n"
            f"Please ensure all required variables are set in your environment or .env file.\n"
            f"Refer to .env.example for required variables."
        )

    return Settings(
        telegram_bot_token=telegram_bot_token,
        allowed_telegram_user_ids=allowed_telegram_user_ids,
        instagram_access_token=instagram_access_token,
        instagram_business_account_id=instagram_business_account_id,
        azure_storage_connection_string=azure_storage_connection_string,
        log_level=log_level,
        ytdlp_cookies_path=ytdlp_cookies_path,
        ytdlp_proxy_url=ytdlp_proxy_url,
        ytdlp_player_clients=ytdlp_player_clients,
        default_cover_path=default_cover_path.resolve(),
    )


# Read once at startup
config = _load_and_validate_settings()

# Direct module-level convenience exports
TELEGRAM_BOT_TOKEN = config.telegram_bot_token
ALLOWED_TELEGRAM_USER_IDS = config.allowed_telegram_user_ids
INSTAGRAM_ACCESS_TOKEN = config.instagram_access_token
INSTAGRAM_BUSINESS_ACCOUNT_ID = config.instagram_business_account_id
AZURE_STORAGE_CONNECTION_STRING = config.azure_storage_connection_string
LOG_LEVEL = config.log_level
YTDLP_COOKIES_PATH = config.ytdlp_cookies_path
YTDLP_PROXY_URL = config.ytdlp_proxy_url
YTDLP_PLAYER_CLIENTS = config.ytdlp_player_clients
DEFAULT_COVER_PATH = config.default_cover_path
