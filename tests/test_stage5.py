"""Automated verification suite for Stage 5: Public Hosting Stage (Azure Blob Storage)."""

import asyncio
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Setup environment before bot imports
MOCK_CONN_STR = (
    "DefaultEndpointsProtocol=https;"
    "AccountName=testaccount;"
    "AccountKey=dGVzdGFjY291bnRrZXk=;"
    "EndpointSuffix=core.windows.net"
)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ")
os.environ.setdefault("ALLOWED_TELEGRAM_USER_IDS", "735006720")
os.environ.setdefault("INSTAGRAM_ACCESS_TOKEN", "mock_access_token")
os.environ.setdefault("INSTAGRAM_BUSINESS_ACCOUNT_ID", "17841400000000000")
os.environ.setdefault("AZURE_STORAGE_CONNECTION_STRING", MOCK_CONN_STR)
os.environ.setdefault("AZURE_BLOB_CONTAINER", "ig-uploads-test")
os.environ.setdefault("LOG_LEVEL", "DEBUG")


def test_sas_url_generation():
    """Verify that generate_sas_url produces a valid read-only SAS URL with 4-hour expiry."""
    from bot.azure_storage import AzureStorageManager

    manager = AzureStorageManager(
        connection_string=MOCK_CONN_STR,
        container_name="ig-uploads-test",
    )

    mock_bsc = MagicMock()
    mock_bsc.account_name = "testaccount"
    mock_cred = MagicMock()
    mock_cred.account_key = "dGVzdGFjY291bnRrZXk="
    mock_bsc.credential = mock_cred
    manager.blob_service_client = mock_bsc

    mock_cc = MagicMock()
    mock_blob_client = MagicMock()
    mock_blob_client.url = "https://testaccount.blob.core.windows.net/ig-uploads-test/test-job.mp4"
    mock_cc.get_blob_client.return_value = mock_blob_client
    manager.container_client = mock_cc

    sas_url, expiry = manager.generate_sas_url("test-job.mp4", expiry_hours=4)

    assert "https://testaccount.blob.core.windows.net/ig-uploads-test/test-job.mp4" in sas_url
    assert "sig=" in sas_url or "sp=" in sas_url
    assert "sp=r" in sas_url  # Read-only permission
    assert expiry > datetime.now(timezone.utc) + timedelta(hours=3, minutes=50)

    print("[PASS] Azure SAS: generate_sas_url creates valid read-only SAS token with 4-hour expiry.")


def test_reachability_check():
    """Verify check_blob_reachability validates public accessibility via HTTP HEAD."""
    from bot.azure_storage import AzureStorageManager

    manager = AzureStorageManager(
        connection_string=MOCK_CONN_STR,
        container_name="ig-uploads-test",
    )

    # 1. Success case (HTTP 200)
    mock_resp_200 = MagicMock(status_code=200, reason_phrase="OK")
    with patch("httpx.AsyncClient.head", new_callable=AsyncMock, return_value=mock_resp_200):
        ok, detail = asyncio.run(manager.check_blob_reachability("https://example.com/test.mp4?sas=token"))
        assert ok is True
        assert "HTTP 200" in detail

    # 2. Failure case (HTTP 403 Forbidden - e.g. bad SAS)
    mock_resp_403 = MagicMock(status_code=403, reason_phrase="AuthenticationFailed")
    with patch("httpx.AsyncClient.head", new_callable=AsyncMock, return_value=mock_resp_403):
        ok, detail = asyncio.run(manager.check_blob_reachability("https://example.com/test.mp4?bad=token"))
        assert ok is False
        assert "HTTP 403" in detail

    print("[PASS] Azure Reachability: HTTP HEAD accurately detects reachable and unreachable SAS URLs.")


def test_video_upload_retry_and_verification():
    """Verify video upload retries on transient errors, verifies size, and generates SAS metadata."""
    from bot.azure_storage import AzureStorageManager

    with tempfile.TemporaryDirectory() as tmpdir:
        test_video = Path(tmpdir) / "render.mp4"
        test_video.write_bytes(b"A" * 1024 * 50)  # 50 KB dummy video
        local_size = test_video.stat().st_size

        manager = AzureStorageManager(
            connection_string=MOCK_CONN_STR,
            container_name="ig-uploads-test",
        )

        mock_blob_client = MagicMock()
        mock_props = MagicMock()
        mock_props.size = local_size
        mock_blob_client.get_blob_properties.return_value = mock_props
        mock_blob_client.url = "https://testaccount.blob.core.windows.net/ig-uploads-test/job-123.mp4"

        # Simulate 1 transient failure followed by success
        attempts = 0
        def upload_side_effect(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionResetError("Transient network drop")
            return None

        mock_blob_client.upload_blob.side_effect = upload_side_effect

        mock_cc = MagicMock()
        mock_cc.get_blob_client.return_value = mock_blob_client
        manager.container_client = mock_cc

        with patch.object(manager, "generate_sas_url", return_value=("https://testaccount.blob.core.windows.net/ig-uploads-test/job-123.mp4?sig=mock", datetime.now(timezone.utc) + timedelta(hours=4))), \
             patch.object(manager, "check_blob_reachability", AsyncMock(return_value=(True, "HTTP 200"))):

            result = asyncio.run(
                manager.upload_video_blob(
                    job_id="job-123",
                    file_path=test_video,
                    max_retries=3,
                    initial_backoff=0.01,
                )
            )

            assert attempts == 2, f"Expected 2 attempts (1 retry), got {attempts}"
            assert result["blob_name"] == "job-123.mp4"
            assert result["size"] == local_size
            assert "job-123.mp4?sig=mock" in result["sas_url"]

    print("[PASS] Azure Upload: Retries transient errors, verifies size match, and outputs complete SAS metadata.")


def test_cached_cover_upload():
    """Verify cover image is uploaded once, cached, and only re-uploaded if local mtime changes."""
    from bot.azure_storage import AzureStorageManager

    with tempfile.TemporaryDirectory() as tmpdir:
        cover_path = Path(tmpdir) / "cover.jpg"
        cover_path.write_bytes(b"MOCK_JPEG_HEADER_AND_DATA")
        local_size = cover_path.stat().st_size

        manager = AzureStorageManager(
            connection_string=MOCK_CONN_STR,
            container_name="ig-uploads-test",
        )

        mock_blob_client = MagicMock()
        mock_props = MagicMock()
        mock_props.size = local_size
        mock_blob_client.get_blob_properties.return_value = mock_props
        mock_blob_client.url = "https://testaccount.blob.core.windows.net/ig-uploads-test/cover.jpg"

        mock_cc = MagicMock()
        mock_cc.get_blob_client.return_value = mock_blob_client
        manager.container_client = mock_cc

        with patch.object(manager, "generate_sas_url", return_value=("https://testaccount.blob.core.windows.net/ig-uploads-test/cover.jpg?sig=coversas", datetime.now(timezone.utc) + timedelta(hours=24))), \
             patch.object(manager, "check_blob_reachability", AsyncMock(return_value=(True, "HTTP 200"))):

            # 1. First upload
            res1 = asyncio.run(manager.ensure_cover_uploaded(cover_path))
            assert mock_blob_client.upload_blob.call_count == 1
            assert res1["blob_name"] == "cover.jpg"

            # 2. Second upload with UNCHANGED file -> Must use cache (upload_blob call count remains 1)
            res2 = asyncio.run(manager.ensure_cover_uploaded(cover_path))
            assert mock_blob_client.upload_blob.call_count == 1
            assert res2["sas_url"] == res1["sas_url"]

            # 3. Modify file mtime -> Must re-upload (upload_blob call count becomes 2)
            new_mtime = cover_path.stat().st_mtime + 10.0
            os.utime(cover_path, (new_mtime, new_mtime))
            res3 = asyncio.run(manager.ensure_cover_uploaded(cover_path))
            assert mock_blob_client.upload_blob.call_count == 2

    print("[PASS] Azure Cover Cache: Uploads cover once, caches SAS URL, and re-uploads only when mtime changes.")


def test_blob_deletion_and_retention_sweep():
    """Verify per-job video deletion and 24h retention sweep (preserving cover images)."""
    from bot.azure_storage import AzureStorageManager

    manager = AzureStorageManager(
        connection_string=MOCK_CONN_STR,
        container_name="ig-uploads-test",
    )

    mock_blob_client = MagicMock()
    mock_cc = MagicMock()
    mock_cc.get_blob_client.return_value = mock_blob_client
    manager.container_client = mock_cc

    # 1. Test explicit single-job video deletion
    deleted = manager.delete_job_video_blob("job-999")
    assert deleted is True
    mock_blob_client.delete_blob.assert_called_once_with(delete_snapshots="include")

    # 2. Test lifecycle retention cleanup sweep
    now = datetime.now(timezone.utc)
    fresh_video = MagicMock(name="job-fresh.mp4", last_modified=now - timedelta(hours=2))
    fresh_video.name = "job-fresh.mp4"

    stale_video = MagicMock(name="job-stale.mp4", last_modified=now - timedelta(hours=26))
    stale_video.name = "job-stale.mp4"

    stale_cover = MagicMock(name="cover.jpg", last_modified=now - timedelta(hours=48))
    stale_cover.name = "cover.jpg"

    mock_cc.list_blobs.return_value = [fresh_video, stale_video, stale_cover]

    cleaned = manager.cleanup_expired_video_blobs(max_age_hours=24)

    assert "job-stale.mp4" in cleaned
    assert "job-fresh.mp4" not in cleaned
    assert "cover.jpg" not in cleaned, "Cover blob must NEVER be deleted by lifecycle retention!"
    mock_cc.delete_blob.assert_called_once_with("job-stale.mp4", delete_snapshots="include")

    print("[PASS] Azure Deletion & Retention: Single job deletion works and retention sweep strictly protects cover image.")


def test_db_job_store_stage5_transitions():
    """Verify SQLite JobStore records uploading_to_azure, hosted, and azure_upload_failed states."""
    from bot.db import JobStore

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_jobs.db"
        store = JobStore(db_path)

        job_id = str(uuid.uuid4())
        store.create_job(job_id=job_id, user_id=735006720, source_url="https://youtu.be/dummy")

        # Transition: uploading_to_azure
        ok = store.start_azure_upload(job_id)
        assert ok is True
        job = store.get_job(job_id)
        assert job["status"] == "uploading_to_azure"

        # Transition: hosted
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        ok = store.complete_azure_upload(
            job_id=job_id,
            blob_name=f"{job_id}.mp4",
            sas_url=f"https://example.blob.core.windows.net/ig-uploads/{job_id}.mp4?sas=token",
            expires_at=expires_at,
        )
        assert ok is True
        job = store.get_job(job_id)
        assert job["status"] == "hosted"
        assert job["video_blob_name"] == f"{job_id}.mp4"
        assert "sas=token" in job["video_sas_url"]
        assert job["video_sas_expires_at"] == expires_at

        # Transition: fail_azure_upload
        job_id_fail = str(uuid.uuid4())
        store.create_job(job_id=job_id_fail, user_id=735006720, source_url="https://youtu.be/dummy2")
        store.start_azure_upload(job_id_fail)
        ok = store.fail_azure_upload(job_id_fail, "Azure authentication failed: Invalid key")
        assert ok is True
        failed_job = store.get_job(job_id_fail)
        assert failed_job["status"] == "azure_upload_failed"
        assert "Invalid key" in failed_job["error_message"]

    print("[PASS] SQLite JobStore: Stage 5 columns and state transitions recorded accurately.")


def test_status_command_reports_azure_container():
    """Verify /status command checks Azure container health alongside other services."""
    from bot.handlers import status_command

    mock_status_msg = MagicMock()
    mock_status_msg.edit_text = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.reply_text = AsyncMock(return_value=mock_status_msg)
    mock_update = MagicMock(effective_message=mock_msg, effective_user=MagicMock(id=735006720))
    mock_context = MagicMock()

    with patch("bot.handlers._check_instagram", AsyncMock(return_value=(True, "Connected (@memoxz100k)"))), \
         patch("bot.handlers._check_azure", AsyncMock(return_value=(True, "Reachable (Account SKU: Standard_LRS, Container 'ig-uploads': OK)"))):

        asyncio.run(status_command(mock_update, mock_context))

        mock_status_msg.edit_text.assert_called_once()
        report_text = mock_status_msg.edit_text.call_args[0][0]
        assert "Azure Blob Storage" in report_text
        assert "ig-uploads" in report_text
        assert "[PASS]" in report_text

    print("[PASS] Azure Status: /status diagnostic report includes container reachability check.")


if __name__ == "__main__":
    print("--- Running Stage 5 Automated Verification Suite ---")
    test_sas_url_generation()
    test_reachability_check()
    test_video_upload_retry_and_verification()
    test_cached_cover_upload()
    test_blob_deletion_and_retention_sweep()
    test_db_job_store_stage5_transitions()
    test_status_command_reports_azure_container()
    print("--- ALL STAGE 5 AUTOMATED TESTS PASSED ---")
