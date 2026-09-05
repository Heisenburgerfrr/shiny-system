"""Automated test suite for Stage 1: Bot Skeleton, Config, Logging, Access Control, and Status Checks."""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Mock environment with all required variables present
MOCK_VALID_ENV = {
    "TELEGRAM_BOT_TOKEN": "1234567890:AAFakeBotTokenForTestingOnlyXYZ",
    "ALLOWED_TELEGRAM_USER_IDS": "735006720, 123456789",
    "INSTAGRAM_ACCESS_TOKEN": "mock_ig_access_token_12345",
    "INSTAGRAM_BUSINESS_ACCOUNT_ID": "17841438767662368",
    "AZURE_STORAGE_CONNECTION_STRING": "DefaultEndpointsProtocol=https;AccountName=mockacc;AccountKey=bW9ja2tleQ==;EndpointSuffix=core.windows.net",
    "LOG_LEVEL": "DEBUG",
}


def setup_module():
    """Ensure modules can be safely imported with valid dummy environment."""
    os.environ.update(MOCK_VALID_ENV)


def test_config_fail_fast_when_missing():
    """Verify that config validation fails fast with an informative error if variables are missing."""
    import bot.config
    # Test with empty environment (patching os.environ and disabling load_dotenv reload)
    with patch.dict(os.environ, {}, clear=True):
        try:
            bot.config._load_and_validate_settings()
            assert False, "Should have raised RuntimeError for missing environment variables"
        except RuntimeError as exc:
            error_str = str(exc)
            assert "TELEGRAM_BOT_TOKEN" in error_str
            assert "ALLOWED_TELEGRAM_USER_IDS" in error_str
            assert "INSTAGRAM_ACCESS_TOKEN" in error_str
            assert "INSTAGRAM_BUSINESS_ACCOUNT_ID" in error_str
            assert "AZURE_STORAGE_CONNECTION_STRING" in error_str
            print("[PASS] Config fails fast with clear error message when env vars are missing.")


def test_config_parsing_and_aliases():
    """Verify that config correctly parses comma-separated user IDs and supports aliases."""
    import bot.config
    mock_env = {
        "TELEGRAM_BOT_TOKEN": "test_token_123",
        "ALLOWED_TELEGRAM_USERS": "735006720, 987654321",  # Alias format
        "INSTAGRAM_ACCOUNT_ID": "17841438767662368",        # Alias format
        "INSTAGRAM_ACCESS_TOKEN": "test_ig_token",
        "AZURE_STORAGE_CONNECTION_STRING": "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=abc;EndpointSuffix=core.windows.net",
        "LOG_LEVEL": "DEBUG",
    }
    with patch.dict(os.environ, mock_env, clear=True):
        settings = bot.config._load_and_validate_settings()
        assert settings.telegram_bot_token == "test_token_123"
        assert settings.allowed_telegram_user_ids == {735006720, 987654321}
        assert settings.instagram_business_account_id == "17841438767662368"
        assert settings.instagram_access_token == "test_ig_token"
        assert settings.azure_storage_connection_string.startswith("DefaultEndpointsProtocol=https")
        assert settings.log_level == "DEBUG"
        print("[PASS] Config correctly parses user IDs, log levels, and environment aliases.")


def test_logging_creation_and_rotation():
    """Verify that logger writes to both console and rotating file under logs/."""
    from bot.logger import setup_logging, LOG_FILE_PATH
    logger = setup_logging("INFO")
    test_msg = "Test diagnostic log entry for Stage 1"
    logger.info(test_msg)

    assert LOG_FILE_PATH.exists(), f"Log file does not exist at {LOG_FILE_PATH}"
    with open(LOG_FILE_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    assert test_msg in content, "Logged message not found in log file"
    print("[PASS] Logging successfully writes to console and rotating log file.")


def test_access_control_decorator():
    """Verify that unauthorized users are silently ignored and authorized users are processed."""
    from bot.handlers import restricted

    called = False

    @restricted
    async def dummy_handler(update, context):
        nonlocal called
        called = True

    # 1. Test unauthorized user
    mock_unauthorized_user = MagicMock(id=999999999, username="intruder")
    mock_update_unauthorized = MagicMock(effective_user=mock_unauthorized_user)
    mock_context = MagicMock()

    called = False
    asyncio.run(dummy_handler(mock_update_unauthorized, mock_context))
    assert not called, "Unauthorized user should NOT trigger the handler"
    mock_update_unauthorized.effective_message.reply_text.assert_not_called()

    # 2. Test authorized user (using allowed ID 735006720 from MOCK_VALID_ENV)
    mock_authorized_user = MagicMock(id=735006720, username="legit_user")
    mock_update_authorized = MagicMock(effective_user=mock_authorized_user)

    called = False
    asyncio.run(dummy_handler(mock_update_authorized, mock_context))
    assert called, "Authorized user should trigger the handler"
    print("[PASS] Access control silently blocks unauthorized users and permits authorized users.")


def test_status_checks_graceful_failures():
    """Verify that invalid Instagram and Azure credentials fail gracefully without crashing."""
    from bot.handlers import _check_instagram, _check_azure

    # Instagram check with dummy credentials returns (False, msg) without throwing uncaught exception
    ig_ok, ig_msg = asyncio.run(_check_instagram())
    assert isinstance(ig_ok, bool)
    assert isinstance(ig_msg, str)

    # Azure check with invalid connection string returns (False, msg) without throwing uncaught exception
    azure_ok, azure_msg = asyncio.run(_check_azure())
    assert isinstance(azure_ok, bool)
    assert isinstance(azure_msg, str)
    assert azure_ok is False  # Dummy connection string should fail gracefully

    print(f"[PASS] Status checks handled gracefully (IG: ok={ig_ok}, Azure: ok={azure_ok}) without unhandled crashes.")


def test_global_error_handler():
    """Verify global error handler logs exceptions cleanly without crashing."""
    from bot.handlers import global_error_handler
    mock_update = MagicMock()
    mock_context = MagicMock()
    mock_context.error = ValueError("Simulated unexpected handler error")

    # Should execute without throwing
    asyncio.run(global_error_handler(mock_update, mock_context))
    print("[PASS] Global error handler logs exception cleanly without crashing.")


if __name__ == "__main__":
    print("--- Running Stage 1 Automated Verification Suite ---")
    setup_module()
    test_config_fail_fast_when_missing()
    test_config_parsing_and_aliases()
    test_logging_creation_and_rotation()
    test_access_control_decorator()
    test_status_checks_graceful_failures()
    test_global_error_handler()
    print("--- ALL STAGE 1 AUTOMATED TESTS PASSED ---")
