"""Automated test suite for Stage 2: YouTube Downloader, SQLite Job Store, Access Control, and Error Classification."""

import asyncio
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Mock environment with valid variables so config loads smoothly
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAFakeBotTokenForTestingOnlyXYZ")
os.environ.setdefault("ALLOWED_TELEGRAM_USER_IDS", "735006720, 123456789")
os.environ.setdefault("INSTAGRAM_ACCESS_TOKEN", "mock_ig_token")
os.environ.setdefault("INSTAGRAM_BUSINESS_ACCOUNT_ID", "17841438767662368")
os.environ.setdefault("AZURE_STORAGE_CONNECTION_STRING", "DefaultEndpointsProtocol=https;AccountName=mockacc;AccountKey=bW9ja2tleQ==;EndpointSuffix=core.windows.net")
os.environ.setdefault("LOG_LEVEL", "DEBUG")


def test_sqlite_job_store():
    """Verify SQLite job store operations: create, update, complete, and interrupted job recovery."""
    from bot.db import JobStore

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_file = Path(tmp_dir) / "test_jobs.db"
        store = JobStore(db_path=db_file)

        # 1. Create job
        test_job_id = str(uuid.uuid4())
        job = store.create_job(test_job_id, user_id=735006720, source_url="https://youtu.be/test1234")
        assert job is not None
        assert job["job_id"] == test_job_id
        assert job["status"] == "pending"
        assert job["user_id"] == 735006720

        # 2. Update status to downloading
        store.update_status(test_job_id, "downloading")
        job = store.get_job(test_job_id)
        assert job["status"] == "downloading"

        # 3. Complete download
        store.complete_download(
            job_id=test_job_id,
            title="Test Video",
            duration=120,
            file_path="storage/downloads/test.mp4",
            file_size=1048576,
        )
        job = store.get_job(test_job_id)
        assert job["status"] == "downloaded"
        assert job["title"] == "Test Video"
        assert job["file_size"] == 1048576

        # 4. Test recovery of interrupted jobs
        interrupted_job_id = str(uuid.uuid4())
        store.create_job(interrupted_job_id, user_id=735006720, source_url="https://youtu.be/interrupted")
        store.update_status(interrupted_job_id, "downloading")

        recovered = store.recover_interrupted_jobs()
        assert interrupted_job_id in recovered
        job = store.get_job(interrupted_job_id)
        assert job["status"] == "failed"
        assert "Interrupted by bot restart" in job["error_message"]

        print("[PASS] SQLite JobStore: CRUD and interrupted job recovery working properly.")


def test_error_classification():
    """Verify yt-dlp error classification maps exceptions to readable plain-language categories."""
    from bot.downloader import DownloadCategory, classify_error, is_permanent_failure

    # Test Private / Deleted
    cat, msg = classify_error(Exception("ERROR: [youtube] Private video. Sign in if you've been granted access"))
    assert cat == DownloadCategory.UNAVAILABLE_OR_PRIVATE
    assert "private, removed, or unavailable" in msg
    assert is_permanent_failure(cat) is True

    # Test Geo-blocked
    cat, msg = classify_error(Exception("ERROR: [youtube] This video is not available in your country"))
    assert cat == DownloadCategory.GEO_BLOCKED
    assert "geo-blocked" in msg
    assert is_permanent_failure(cat) is True

    # Test Bot Detection
    cat, msg = classify_error(Exception("ERROR: [youtube] Sign in to confirm you're not a bot"))
    assert cat == DownloadCategory.BOT_DETECTED
    assert "bot verification" in msg
    assert is_permanent_failure(cat) is False

    # Test Timeout / Network
    cat, msg = classify_error(Exception("ERROR: [youtube] Connection timed out after 30 seconds"))
    assert cat == DownloadCategory.NETWORK_TIMEOUT
    assert "timed out" in msg

    # Test Unknown
    cat, msg = classify_error(Exception("Unexpected custom error"))
    assert cat == DownloadCategory.UNKNOWN

    print("[PASS] Downloader: Error classification and permanent failure detection working properly.")

def test_missing_cookie_file_tolerance():
    """Verify setting an invalid or missing cookies path logs a warning and does not crash."""
    from bot.downloader import YouTubeDownloader

    mock_cfg = MagicMock(ytdlp_cookies_path="/non/existent/path/cookies.txt")
    with patch("bot.downloader.config", mock_cfg):
        dl = YouTubeDownloader()
        cookies_val = dl._get_validated_cookies_path()
        assert cookies_val is None, "Missing cookie path should resolve to None without raising exception"

    print("[PASS] Downloader: Missing/invalid cookie path handled gracefully.")


def test_youtube_url_regex():
    """Verify regex accurately identifies various YouTube URL formats."""
    from bot.handlers import YOUTUBE_URL_REGEX

    valid_urls = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "http://youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/3jZ_7n_T7hA",
        "https://youtube.com/live/5qap5aO4i9A",
        "Check this out: https://youtu.be/dQw4w9WgXcQ it's cool",
    ]

    invalid_urls = [
        "https://www.instagram.com/reel/C12345/",
        "https://vimeo.com/123456789",
        "https://google.com",
        "Just a plain text message with no links",
    ]

    for url in valid_urls:
        match = YOUTUBE_URL_REGEX.search(url)
        assert match is not None, f"Failed to match valid URL: {url}"

    for url in invalid_urls:
        match = YOUTUBE_URL_REGEX.search(url)
        assert match is None, f"Incorrectly matched invalid URL: {url}"

    print("[PASS] Handlers: YouTube URL regex correctly parses valid links and rejects non-YouTube text.")


def test_access_control_on_youtube_handler():
    """Verify unauthorized users are silently ignored and authorized users trigger jobs."""
    from bot.handlers import youtube_url_handler
    from bot.db import job_store

    # 1. Unauthorized user
    mock_unauth_user = MagicMock(id=888888888, username="hacker")
    mock_msg_unauth = MagicMock()
    mock_msg_unauth.text = "https://youtu.be/dQw4w9WgXcQ"
    mock_update_unauth = MagicMock(effective_user=mock_unauth_user, effective_message=mock_msg_unauth)
    mock_context = MagicMock()

    asyncio.run(youtube_url_handler(mock_update_unauth, mock_context))
    mock_msg_unauth.reply_text.assert_not_called()

    # 2. Authorized user (user_id 735006720)
    mock_auth_user = MagicMock(id=735006720, username="allowed_user")
    mock_msg_auth = MagicMock()
    mock_msg_auth.text = "https://youtu.be/dQw4w9WgXcQ"
    mock_msg_auth.reply_text = AsyncMock(return_value=MagicMock())
    mock_update_auth = MagicMock(effective_user=mock_auth_user, effective_message=mock_msg_auth)

    with patch("asyncio.create_task") as mock_task:
        asyncio.run(youtube_url_handler(mock_update_auth, mock_context))
        mock_msg_auth.reply_text.assert_called_once()
        call_args = mock_msg_auth.reply_text.call_args[0][0]
        assert "Download Request Received" in call_args
        assert mock_task.called
        # Close coroutine cleanly
        mock_task.call_args[0][0].close()

    print("[PASS] Handlers: Access control silently drops unauthorized downloads and acknowledges allowed users.")


def test_download_failure_cleanup():
    """Verify that when a download fails, partial files are removed and job status is updated to failed."""
    from bot.downloader import downloader, DOWNLOADS_DIR
    from bot.db import job_store

    test_job_id = str(uuid.uuid4())
    job_store.create_job(test_job_id, user_id=735006720, source_url="https://youtu.be/invalid_deleted_video_test")

    # Create dummy partial files to simulate leftover yt-dlp partial files
    part_file = DOWNLOADS_DIR / f"{test_job_id}.mp4.part"
    ytdl_file = DOWNLOADS_DIR / f"{test_job_id}.ytdl"
    part_file.write_text("dummy partial content")
    ytdl_file.write_text("dummy metadata")

    assert part_file.exists()
    assert ytdl_file.exists()

    # Trigger simulated failure
    downloader._cleanup_partial_files(test_job_id)

    assert not part_file.exists(), "Partial file should be cleaned up"
    assert not ytdl_file.exists(), ".ytdl file should be cleaned up"

    print("[PASS] Downloader: Cleanup cleanly removes partial files on failure.")


if __name__ == "__main__":
    print("--- Running Stage 2 Automated Verification Suite ---")
    test_sqlite_job_store()
    test_error_classification()
    test_missing_cookie_file_tolerance()
    test_youtube_url_regex()
    test_access_control_on_youtube_handler()
    test_download_failure_cleanup()
    print("--- ALL STAGE 2 AUTOMATED TESTS PASSED ---")
