"""Automated verification suite for Stage 6: Instagram Upload Stage (Graph API)."""

import asyncio
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Setup environment before bot imports
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ")
os.environ.setdefault("ALLOWED_TELEGRAM_USER_IDS", "735006720")
os.environ.setdefault("INSTAGRAM_ACCESS_TOKEN", "EAABmock_token_stage6")
os.environ.setdefault("INSTAGRAM_BUSINESS_ACCOUNT_ID", "17841400000000000")
os.environ.setdefault(
    "AZURE_STORAGE_CONNECTION_STRING",
    "DefaultEndpointsProtocol=https;AccountName=testacc;AccountKey=dGVzdGtleQ==;EndpointSuffix=core.windows.net"
)
os.environ.setdefault("AZURE_BLOB_CONTAINER", "ig-uploads")
os.environ.setdefault("LOG_LEVEL", "DEBUG")


def test_reels_container_creation():
    """Verify create_reels_container calls POST /{id}/media with REELS and SAS URLs."""
    from bot.instagram_publish import InstagramPublisher

    publisher = InstagramPublisher(
        access_token="test_token",
        business_account_id="17841400000000000",
    )

    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"id": "18023456789012345"}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
        container_id = asyncio.run(
            publisher.create_reels_container(
                video_url="https://example.blob.core.windows.net/ig-uploads/job-1.mp4?sas=token",
                caption="Test Reel Caption #meme",
                cover_url="https://example.blob.core.windows.net/ig-uploads/cover.jpg?sas=token",
            )
        )

        assert container_id == "18023456789012345"
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        posted_data = call_args[1]["data"]
        assert posted_data["media_type"] == "REELS"
        assert posted_data["video_url"].startswith("https://")
        assert posted_data["cover_url"].startswith("https://")
        assert posted_data["caption"] == "Test Reel Caption #meme"

    print("[PASS] Instagram Container: POST /{id}/media created successfully with REELS payload.")


def test_container_polling_and_throttled_progress():
    """Verify polling waits through IN_PROGRESS and completes on FINISHED with progress callbacks."""
    from bot.instagram_publish import InstagramPublisher

    publisher = InstagramPublisher(
        access_token="test_token",
        business_account_id="17841400000000000",
        poll_interval=0.01,
        max_poll_timeout=2.0,
    )

    # 2 IN_PROGRESS responses followed by 1 FINISHED response
    responses = [
        MagicMock(status_code=200, json=lambda: {"status_code": "IN_PROGRESS", "status": "Transcoding"}),
        MagicMock(status_code=200, json=lambda: {"status_code": "IN_PROGRESS", "status": "Uploading"}),
        MagicMock(status_code=200, json=lambda: {"status_code": "FINISHED", "status": "Ready"}),
    ]
    idx = 0
    async def mock_get(*args, **kwargs):
        nonlocal idx
        resp = responses[min(idx, len(responses) - 1)]
        idx += 1
        return resp

    progress_ticks = []
    async def on_progress(code, elapsed):
        progress_ticks.append((code, elapsed))

    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        res = asyncio.run(
            publisher.poll_container_status(
                container_id="18023456789012345",
                progress_callback=on_progress,
            )
        )
        assert res == "18023456789012345"
        assert len(progress_ticks) >= 2
        assert progress_ticks[0][0] == "IN_PROGRESS"

    print("[PASS] Instagram Polling: Successfully waited through IN_PROGRESS and terminated on FINISHED.")


def test_container_error_and_timeout_handling():
    """Verify ERROR status and timeouts raise domain exceptions without hanging."""
    from bot.instagram_publish import (
        InstagramPublisher,
        InstagramPublishError,
        InstagramTimeoutError,
    )

    # 1. Test ERROR status
    publisher = InstagramPublisher(
        access_token="test_token",
        business_account_id="17841400000000000",
        poll_interval=0.01,
        max_poll_timeout=5.0,
    )
    error_resp = MagicMock(
        status_code=200,
        json=lambda: {"status_code": "ERROR", "status": "Video resolution not supported"}
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=error_resp):
        try:
            asyncio.run(publisher.poll_container_status("error_container"))
            assert False, "Expected InstagramPublishError on ERROR status"
        except InstagramPublishError as exc:
            assert "Video resolution not supported" in str(exc)

    # 2. Test Timeout
    timeout_publisher = InstagramPublisher(
        access_token="test_token",
        business_account_id="17841400000000000",
        poll_interval=0.01,
        max_poll_timeout=0.02,  # Ultra short timeout
    )
    in_prog_resp = MagicMock(
        status_code=200,
        json=lambda: {"status_code": "IN_PROGRESS", "status": "Stuck in processing"}
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=in_prog_resp):
        try:
            asyncio.run(timeout_publisher.poll_container_status("stuck_container"))
            assert False, "Expected InstagramTimeoutError on timeout"
        except InstagramTimeoutError as exc:
            assert "timed out" in str(exc).lower()

    print("[PASS] Instagram Error/Timeout: ERROR statuses and timeouts cleanly raise domain exceptions.")


def test_token_expired_critical_classification():
    """Verify OAuth code 190 triggers InstagramTokenExpiredError for critical alerting."""
    from bot.instagram_publish import InstagramPublisher, InstagramTokenExpiredError

    publisher = InstagramPublisher(
        access_token="expired_token",
        business_account_id="17841400000000000",
    )

    mock_err_resp = MagicMock(
        status_code=400,
        json=lambda: {
            "error": {
                "message": "Error validating access token: Session has expired on...",
                "type": "OAuthException",
                "code": 190,
                "error_subcode": 463,
            }
        }
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_err_resp):
        try:
            asyncio.run(
                publisher.create_reels_container(
                    video_url="https://example.com/video.mp4",
                    caption="Caption",
                )
            )
            assert False, "Expected InstagramTokenExpiredError on OAuth code 190"
        except InstagramTokenExpiredError as exc:
            assert "expired or invalid" in str(exc).lower()
            assert "190" in str(exc)

    print("[PASS] Instagram Auth: OAuth code 190 correctly classified as InstagramTokenExpiredError.")


def test_end_to_end_publish_and_blob_cleanup():
    """Verify end-to-end publish flow retrieves permalink and deletes the Azure video blob."""
    from bot.instagram_publish import InstagramPublisher
    from bot.db import JobStore

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_jobs.db"
        store = JobStore(db_path)

        job_id = "job-publish-test-01"
        store.create_job(job_id=job_id, user_id=735006720, source_url="https://youtu.be/dummy")
        store.complete_azure_upload(
            job_id=job_id,
            blob_name=f"{job_id}.mp4",
            sas_url="https://test.blob.core.windows.net/ig-uploads/job.mp4?sas=token",
            expires_at="2026-09-06T00:00:00Z",
        )

        publisher = InstagramPublisher(
            access_token="valid_token",
            business_account_id="17841400000000000",
            poll_interval=0.01,
        )

        with patch("bot.instagram_publish.job_store", store), \
             patch.object(publisher, "create_reels_container", AsyncMock(return_value="container-111")), \
             patch.object(publisher, "poll_container_status", AsyncMock(return_value="container-111")), \
             patch.object(publisher, "publish_container", AsyncMock(return_value="media-222")), \
             patch.object(publisher, "get_media_permalink", AsyncMock(return_value="https://www.instagram.com/reel/DE-TEST-123/")), \
             patch("bot.instagram_publish.azure_storage_manager.delete_job_video_blob") as mock_delete_blob:

            result = asyncio.run(
                publisher.publish_reel(
                    job_id=job_id,
                    video_url="https://test.blob.core.windows.net/ig-uploads/job.mp4?sas=token",
                    caption="Amazing Reel Caption",
                    cover_url="https://test.blob.core.windows.net/ig-uploads/cover.jpg?sas=token",
                )
            )

            assert result["container_id"] == "container-111"
            assert result["media_id"] == "media-222"
            assert result["permalink"] == "https://www.instagram.com/reel/DE-TEST-123/"

            # Verify SQLite record updated to published
            job = store.get_job(job_id)
            assert job["status"] == "published"
            assert job["instagram_media_id"] == "media-222"
            assert job["instagram_permalink"] == "https://www.instagram.com/reel/DE-TEST-123/"

            # Verify temporary Azure video blob was cleaned up!
            mock_delete_blob.assert_called_once_with(job_id)

    print("[PASS] Instagram Publish E2E: Published live with permalink and triggered Azure blob cleanup.")


def test_multiline_inline_caption_extraction():
    """Verify inline multiline caption extraction from user Telegram messages."""
    from bot.handlers import YOUTUBE_URL_REGEX

    test_message = """https://www.youtube.com/watch?v=dQw4w9WgXcQ
This is a multiline caption for Instagram!
Second line of caption text.
#viral #memes @memoxz100k"""

    match = YOUTUBE_URL_REGEX.search(test_message)
    assert match is not None
    url = match.group(1).strip()
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in url

    caption_part = test_message.replace(match.group(0), "").strip()
    assert "This is a multiline caption for Instagram!" in caption_part
    assert "Second line of caption text." in caption_part
    assert "#viral #memes @memoxz100k" in caption_part

    print("[PASS] Telegram Caption: Multiline inline caption cleanly extracted alongside YouTube link.")


def test_rate_limit_rolling_24h_count():
    """Verify 24-hour rate limit calculation and enforcement."""
    from bot.db import JobStore
    from bot.instagram_publish import InstagramPublisher

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_jobs.db"
        store = JobStore(db_path)

        now = datetime.now(timezone.utc)
        # Insert 3 published jobs in last 24h
        for i in range(3):
            jid = f"job-recent-{i}"
            store.create_job(jid, 735006720, "https://youtu.be/dummy")
            store.complete_publishing(jid, f"media-{i}", f"https://ig/{i}")

        # Insert 1 published job older than 24h (26h ago)
        old_jid = "job-old"
        store.create_job(old_jid, 735006720, "https://youtu.be/dummy")
        store.complete_publishing(old_jid, "media-old", "https://ig/old")
        old_time = (now - timedelta(hours=26)).isoformat()
        with store._connection() as conn:
            conn.execute("UPDATE jobs SET updated_at = ? WHERE job_id = ?", (old_time, old_jid))
            conn.commit()

        count = store.count_published_in_last_24h()
        assert count == 3, f"Expected 3 recent published jobs, got {count}"

        publisher = InstagramPublisher()
        with patch("bot.instagram_publish.job_store", store), \
             patch("bot.instagram_publish.INSTAGRAM_PUBLISH_RATE_LIMIT", 3):
            allowed, curr = publisher.check_rate_limit()
            assert allowed is False
            assert curr == 3

    print("[PASS] Instagram Rate Limit: Rolling 24h publication count calculated and capped properly.")


if __name__ == "__main__":
    print("--- Running Stage 6 Automated Verification Suite ---")
    test_reels_container_creation()
    test_container_polling_and_throttled_progress()
    test_container_error_and_timeout_handling()
    test_token_expired_critical_classification()
    test_end_to_end_publish_and_blob_cleanup()
    test_multiline_inline_caption_extraction()
    test_rate_limit_rolling_24h_count()
    print("--- ALL STAGE 6 AUTOMATED TESTS PASSED ---")
