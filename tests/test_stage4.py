"""Automated test suite for Stage 4: Default Cover Image Handling and Instagram Reels Compliance."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

# Ensure root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Mock environment with required variables
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAFakeBotTokenForTestingOnlyXYZ")
os.environ.setdefault("ALLOWED_TELEGRAM_USER_IDS", "735006720, 123456789")
os.environ.setdefault("INSTAGRAM_ACCESS_TOKEN", "mock_ig_token")
os.environ.setdefault("INSTAGRAM_BUSINESS_ACCOUNT_ID", "17841438767662368")
os.environ.setdefault("AZURE_STORAGE_CONNECTION_STRING", "DefaultEndpointsProtocol=https;AccountName=mockacc;AccountKey=bW9ja2tleQ==;EndpointSuffix=core.windows.net")
os.environ.setdefault("LOG_LEVEL", "DEBUG")


def test_cover_inspection_compliance():
    """Verify inspection detects valid 9:16 Reels covers and flags invalid aspect ratios or formats."""
    from bot.cover import inspect_cover_image

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 1. Valid 1080x1920 (9:16) PNG
        valid_png = tmp_path / "valid_cover.png"
        img_valid = Image.new("RGB", (1080, 1920), color="blue")
        img_valid.save(valid_png, "PNG")

        res_valid = inspect_cover_image(valid_png)
        assert res_valid.is_valid is True
        assert res_valid.width == 1080
        assert res_valid.height == 1920
        assert "9:16" in res_valid.details

        # 2. Invalid 1920x1080 (16:9) JPEG
        invalid_16_9 = tmp_path / "landscape_cover.jpg"
        img_invalid = Image.new("RGB", (1920, 1080), color="red")
        img_invalid.save(invalid_16_9, "JPEG")

        res_invalid = inspect_cover_image(invalid_16_9)
        assert res_invalid.is_valid is False
        assert "Invalid aspect ratio" in res_invalid.status_summary
        assert "1.78" in res_invalid.details

        # 3. Missing file
        missing_file = tmp_path / "non_existent.png"
        res_missing = inspect_cover_image(missing_file)
        assert res_missing.is_valid is False
        assert "Missing" in res_missing.status_summary

        print("[PASS] Cover: Compliance inspection accurately validates 9:16 aspect ratio and detects invalid files.")


def test_fail_fast_on_missing_cover():
    """Verify bot fails fast at startup with clear error message if default cover image is missing."""
    with patch.dict(os.environ, {"DEFAULT_COVER_PATH": "missing_nonexistent_cover.png"}):
        try:
            from bot.config import _load_and_validate_settings
            _load_and_validate_settings()
            assert False, "Should have raised RuntimeError when cover image is missing"
        except RuntimeError as exc:
            err_text = str(exc)
            assert "Missing required default cover image" in err_text
            assert "missing_nonexistent_cover.png" in err_text
            print("[PASS] Cover: Bot configuration fails fast at startup when cover image is missing.")


def test_status_command_reports_cover_health():
    """Verify /status command checks and reports the default cover image health."""
    import asyncio
    from bot.handlers import status_command

    from unittest.mock import AsyncMock

    mock_status_msg = MagicMock()
    mock_status_msg.edit_text = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.reply_text = AsyncMock(return_value=mock_status_msg)
    mock_update = MagicMock(effective_message=mock_msg, effective_user=MagicMock(id=735006720))
    mock_context = MagicMock()

    with patch("bot.handlers._check_instagram", AsyncMock(return_value=(True, "Connected (@test)"))), \
         patch("bot.handlers._check_azure", AsyncMock(return_value=(True, "Reachable"))):
        asyncio.run(status_command(mock_update, mock_context))

        mock_status_msg.edit_text.assert_called_once()
        report_text = mock_status_msg.edit_text.call_args[0][0]
        assert "Default Cover Image" in report_text
        assert "[PASS]" in report_text
        print("[PASS] Cover: /status command reports Default Cover Image status alongside external checks.")


def test_actual_project_cover():
    """Verify that the actual project root cover.jpg / cover.png is valid."""
    from bot.config import DEFAULT_COVER_PATH
    from bot.cover import inspect_cover_image

    assert Path(DEFAULT_COVER_PATH).exists(), f"Default cover file does not exist: {DEFAULT_COVER_PATH}"
    result = inspect_cover_image(Path(DEFAULT_COVER_PATH))
    assert result.is_valid is True, f"Actual project cover failed validation: {result.details}"
    print(f"[PASS] Cover: Actual project cover verified: {result.details}")


if __name__ == "__main__":
    print("--- Running Stage 4 Automated Verification Suite ---")
    test_cover_inspection_compliance()
    test_fail_fast_on_missing_cover()
    test_status_command_reports_cover_health()
    test_actual_project_cover()
    print("--- ALL STAGE 4 AUTOMATED TESTS PASSED ---")
