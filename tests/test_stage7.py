"""Automated verification suite for Stage 7: Cleanup, State, and Error Recovery."""

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
os.environ.setdefault("INSTAGRAM_ACCESS_TOKEN", "EAABmock_token_stage7")
os.environ.setdefault("INSTAGRAM_BUSINESS_ACCOUNT_ID", "17841400000000000")
os.environ.setdefault(
    "AZURE_STORAGE_CONNECTION_STRING",
    "DefaultEndpointsProtocol=https;AccountName=testacc;AccountKey=dGVzdGtleQ==;EndpointSuffix=core.windows.net"
)
os.environ.setdefault("AZURE_BLOB_CONTAINER", "ig-uploads")
os.environ.setdefault("LOG_LEVEL", "DEBUG")

from bot.config import config
from bot.db import DOWNLOADS_DIR, PROCESSED_DIR, STORAGE_DIR, JobStore
from bot.recovery import (
    StartupRecoveryManager,
    StorageCleaner,
    SystemicFailureTracker,
    TaskRegistry,
    failure_tracker,
    startup_recovery,
    storage_cleaner,
    task_registry,
)


def test_startup_recovery_and_report():
    """Verify startup recovery handles each non-terminal state and prevents duplicate publishing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db_path = Path(tmpdir) / "test_jobs.db"
        store = JobStore(db_path=test_db_path)

        # 1. Interrupted during download
        job_dl = str(uuid.uuid4())
        store.create_job(job_dl, 735006720, "https://youtube.com/watch?v=dl1")
        store.update_status(job_dl, "downloading")

        # 2. Interrupted during processing
        job_proc = str(uuid.uuid4())
        store.create_job(job_proc, 735006720, "https://youtube.com/watch?v=proc1")
        store.update_status(job_proc, "processing")

        # 3. Interrupted during Azure upload
        job_az = str(uuid.uuid4())
        store.create_job(job_az, 735006720, "https://youtube.com/watch?v=az1")
        store.start_azure_upload(job_az)

        # 4. Interrupted during Instagram publishing
        job_pub = str(uuid.uuid4())
        store.create_job(job_pub, 735006720, "https://youtube.com/watch?v=pub1")
        store.complete_azure_upload(job_pub, f"{job_pub}.mp4", "https://mock.blob/vid?sas", "2026-12-31T00:00:00Z")
        store.start_publishing(job_pub, "Caption for pub1")

        # 5. Job awaiting caption (should be preserved)
        job_cap = str(uuid.uuid4())
        store.create_job(job_cap, 735006720, "https://youtube.com/watch?v=cap1")
        store.set_awaiting_caption(job_cap)

        # 6. Completed published job (terminal - should not be touched)
        job_done = str(uuid.uuid4())
        store.create_job(job_done, 735006720, "https://youtube.com/watch?v=done1")
        store.complete_publishing(job_done, "media_123", "https://instagram.com/reel/done1")

        with patch("bot.recovery.job_store", store):
            recovered = StartupRecoveryManager.scan_and_recover_jobs()

        # Check recovery results
        recovered_dict = {r["job_id"]: r for r in recovered}
        assert len(recovered) == 5, f"Expected 5 non-terminal jobs recovered, got {len(recovered)}"

        # Check download job
        assert store.get_job(job_dl)["status"] == "download_failed"

        # Check processing job
        assert store.get_job(job_proc)["status"] == "processing_failed"

        # Check azure job
        assert store.get_job(job_az)["status"] == "azure_upload_failed"

        # Check publishing job - MUST require manual review to prevent duplicate posts!
        pub_record = store.get_job(job_pub)
        assert pub_record["status"] == "publish_failed"
        assert "duplicate posts" in pub_record["error_message"].lower()
        assert recovered_dict[job_pub]["needs_manual_review"] is True

        # Check awaiting caption job
        assert store.get_job(job_cap)["status"] == "awaiting_caption"

        # Check published job was unaffected
        assert store.get_job(job_done)["status"] == "published"

        # Check Telegram notification formatting
        report = StartupRecoveryManager.format_recovery_report(recovered)
        assert report is not None
        assert "Startup Recovery Report" in report
        assert job_pub in report
        assert "⚠️" in report  # Indicator for manual review job

    print("[PASS] Startup Recovery: Safely cleans interrupted jobs, flags publishing for manual review, and formats report.")


def test_storage_cleaner_immediate_and_periodic():
    """Verify immediate per-job local cleanup and 48h periodic safety sweep."""
    cleaner = StorageCleaner()
    temp_dir = STORAGE_DIR / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    job_id = f"test-clean-{uuid.uuid4()}"
    dl_file = DOWNLOADS_DIR / f"{job_id}.mp4"
    proc_file = PROCESSED_DIR / f"{job_id}.mp4"
    tmp_file = temp_dir / f"{job_id}.wav"

    dl_file.write_text("raw-data")
    proc_file.write_text("processed-data")
    tmp_file.write_text("audio-data")

    assert dl_file.exists() and proc_file.exists() and tmp_file.exists()

    # 1. Immediate cleanup
    deleted = cleaner.cleanup_job_local_files(job_id)
    assert len(deleted) == 3
    assert not dl_file.exists()
    assert not proc_file.exists()
    assert not tmp_file.exists()

    # 2. Periodic safety net sweep
    # Create an orphaned stale file (age > 48h)
    old_file = temp_dir / f"orphan_old_{uuid.uuid4()}.tmp"
    old_file.write_text("old-orphan")
    past_time = time.time() - (50 * 3600)  # 50 hours ago
    os.utime(old_file, (past_time, past_time))

    # Create a fresh file (age < 48h)
    fresh_file = temp_dir / f"orphan_fresh_{uuid.uuid4()}.tmp"
    fresh_file.write_text("fresh-orphan")

    # Create a file belonging to an active job
    active_jid = f"active-{uuid.uuid4()}"
    active_file = DOWNLOADS_DIR / f"{active_jid}.mp4"
    active_file.write_text("active-in-progress")
    os.utime(active_file, (past_time, past_time))  # Even if old, it is active!

    with patch("bot.recovery.job_store.list_in_progress_jobs", return_value=[{"job_id": active_jid}]):
        swept = cleaner.periodic_storage_sweep(max_age_hours=48)

    assert old_file in swept
    assert not old_file.exists()
    assert fresh_file.exists()  # Fresh file preserved
    assert active_file.exists()  # Active job file preserved

    # Clean up test files
    fresh_file.unlink(missing_ok=True)
    active_file.unlink(missing_ok=True)

    print("[PASS] Storage Cleaner: Immediate local cleanup and 48-hour periodic safety sweep verified.")


def test_systemic_failure_tracking():
    """Verify consecutive failure alerts (threshold=3) and immediate token expiration alert."""
    tracker = SystemicFailureTracker(threshold=3)

    # 1. First failure -> no alert
    alert1 = tracker.record_failure("download", "job-1", "YouTube bot check")
    assert alert1 is None

    # 2. Second failure -> no alert
    alert2 = tracker.record_failure("download", "job-2", "YouTube bot check")
    assert alert2 is None

    # 3. Third failure -> triggers systemic alert!
    alert3 = tracker.record_failure("download", "job-3", "YouTube bot check")
    assert alert3 is not None
    assert "SYSTEMIC ALERT" in alert3
    assert "3 consecutive failures" in alert3
    assert "job-3" in alert3

    # Counter should reset after alerting
    assert tracker._consecutive_failures["download"] == 0

    # 4. Success resets counter
    tracker.record_failure("processing", "job-4", "FFmpeg error")
    assert tracker._consecutive_failures["processing"] == 1
    tracker.record_success("processing")
    assert tracker._consecutive_failures["processing"] == 0

    # 5. Instagram token failure -> IMMEDIATE critical alert on first failure
    token_alert = tracker.record_failure("token", "job-5", "Session expired")
    assert token_alert is not None
    assert "CRITICAL SYSTEMIC ALERT: Instagram Token Expired" in token_alert

    print("[PASS] Failure Tracking: 3 consecutive failure threshold and instant token expiration alerts verified.")


def test_task_registry_and_cancellation():
    """Verify task registration and cancellation of running asyncio.Task."""
    registry = TaskRegistry()

    async def sample_task():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            return "cancelled"

    async def run_test():
        task = asyncio.create_task(sample_task())
        registry.register_task("job-task-1", task)

        assert registry.is_task_running("job-task-1") is True

        cancelled = registry.cancel_task("job-task-1")
        assert cancelled is True
        assert registry.is_task_running("job-task-1") is False

        await asyncio.sleep(0.01)
        assert task.cancelled()

    asyncio.run(run_test())
    print("[PASS] Task Registry: Registration, running status check, and task cancellation verified.")


def test_operational_commands():
    """Verify /jobs, /job, /cancel, and /retry commands."""
    from bot.handlers import cancel_command, job_detail_command, jobs_command, retry_command

    with tempfile.TemporaryDirectory() as tmpdir:
        test_db_path = Path(tmpdir) / "test_jobs.db"
        store = JobStore(db_path=test_db_path)

        # Create active job
        jid_active = str(uuid.uuid4())
        store.create_job(jid_active, 735006720, "https://youtube.com/watch?v=active1")
        store.update_status(jid_active, "downloading")

        # Create published job
        jid_published = str(uuid.uuid4())
        store.create_job(jid_published, 735006720, "https://youtube.com/watch?v=pub1")
        store.complete_publishing(jid_published, "media_789", "https://instagram.com/reel/123")

        # 1. Test /jobs command
        mock_msg = AsyncMock()
        update = MagicMock()
        update.effective_user.id = 735006720
        update.effective_message = mock_msg
        context = MagicMock()

        with patch("bot.handlers.job_store", store):
            asyncio.run(jobs_command(update, context))

        mock_msg.reply_text.assert_called_once()
        jobs_reply = mock_msg.reply_text.call_args[0][0]
        assert "Active In-Progress Jobs" in jobs_reply
        assert jid_active[:8] in jobs_reply

        # 2. Test /job <job_id> telemetry detail command
        mock_msg.reset_mock()
        context.args = [jid_active]
        with patch("bot.handlers.job_store", store):
            asyncio.run(job_detail_command(update, context))

        mock_msg.reply_text.assert_called_once()
        detail_reply = mock_msg.reply_text.call_args[0][0]
        assert "Job Details" in detail_reply
        assert jid_active in detail_reply
        assert "downloading" in detail_reply

        # 3. Test /cancel on already-published job -> MUST BE REFUSED
        mock_msg.reset_mock()
        context.args = [jid_published]
        with patch("bot.handlers.job_store", store):
            asyncio.run(cancel_command(update, context))

        mock_msg.reply_text.assert_called_once()
        cancel_pub_reply = mock_msg.reply_text.call_args[0][0]
        assert "Cannot cancel" in cancel_pub_reply
        assert "already published" in cancel_pub_reply

        # 4. Test /cancel on active job -> CANCELLED
        mock_msg.reset_mock()
        context.args = [jid_active]
        with patch("bot.handlers.job_store", store):
            asyncio.run(cancel_command(update, context))

        mock_msg.reply_text.assert_called_once()
        cancel_reply = mock_msg.reply_text.call_args[0][0]
        assert "Job Cancelled" in cancel_reply
        assert store.get_job(jid_active)["status"] == "cancelled"

        # 5. Test /retry resumption stage calculation
        jid_failed = str(uuid.uuid4())
        store.create_job(jid_failed, 735006720, "https://youtube.com/watch?v=fail1")
        store.update_status(jid_failed, "processing_failed", "transient error")

        # Create dummy processed file to verify it resumes from upload_to_azure!
        dummy_proc = PROCESSED_DIR / f"{jid_failed}.mp4"
        dummy_proc.write_text("dummy-master")

        try:
            with patch("bot.handlers.job_store", store):
                can_resume, stage, _ = store.get_job_resumption_stage(jid_failed)
                assert can_resume is True
                assert stage == "upload_to_azure", f"Expected 'upload_to_azure', got '{stage}'"

            # Execute /retry command
            mock_msg.reset_mock()
            context.args = [jid_failed]
            with patch("bot.handlers.job_store", store), \
                 patch("asyncio.create_task") as mock_create_task:
                asyncio.run(retry_command(update, context))

            mock_msg.reply_text.assert_called_once()
            retry_reply = mock_msg.reply_text.call_args[0][0]
            assert "Resuming Job" in retry_reply
            assert "upload_to_azure" in retry_reply
            mock_create_task.assert_called_once()

        finally:
            dummy_proc.unlink(missing_ok=True)

    print("[PASS] Operational Commands: /jobs, /job telemetry, /cancel restrictions, and smart /retry verified.")


def test_global_error_handler_resilience():
    """Verify global_error_handler catches and reports errors without terminating."""
    from bot.handlers import global_error_handler

    mock_msg = AsyncMock()
    mock_update = MagicMock()
    mock_update.effective_message = mock_msg
    mock_context = MagicMock()
    mock_context.error = RuntimeError("Unexpected edge case")

    asyncio.run(global_error_handler(mock_update, mock_context))

    mock_msg.reply_text.assert_called_once()
    err_reply = mock_msg.reply_text.call_args[0][0]
    assert "unexpected error occurred" in err_reply.lower()

    print("[PASS] Global Error Handler: Gracefully catches and alerts without crashing.")


if __name__ == "__main__":
    print("--- Running Stage 7 Automated Verification Suite ---")
    test_startup_recovery_and_report()
    test_storage_cleaner_immediate_and_periodic()
    test_systemic_failure_tracking()
    test_task_registry_and_cancellation()
    test_operational_commands()
    test_global_error_handler_resilience()
    print("--- ALL STAGE 7 AUTOMATED TESTS PASSED ---")
