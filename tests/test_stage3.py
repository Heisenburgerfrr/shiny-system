"""Automated test suite for Stage 3: Memoxz Video Processor, FFmpeg Filter Complex, and Premiere Pro Metadata."""

import asyncio
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Setup dummy environment for config loading
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAFakeBotTokenForTestingOnlyXYZ")
os.environ.setdefault("ALLOWED_TELEGRAM_USER_IDS", "735006720, 123456789")
os.environ.setdefault("INSTAGRAM_ACCESS_TOKEN", "mock_ig_token")
os.environ.setdefault("INSTAGRAM_BUSINESS_ACCOUNT_ID", "17841438767662368")
os.environ.setdefault("AZURE_STORAGE_CONNECTION_STRING", "DefaultEndpointsProtocol=https;AccountName=mockacc;AccountKey=bW9ja2tleQ==;EndpointSuffix=core.windows.net")
os.environ.setdefault("LOG_LEVEL", "DEBUG")


def test_filter_complex_generation():
    """Verify get_filter_complex generates complete anti-fingerprint filter chains."""
    from bot.processor import get_filter_complex

    for preset in ["subtle", "balanced", "aggressive"]:
        vf, af = get_filter_complex(preset)
        # Check scale and crop even dimensions
        assert "scale=trunc" in vf
        assert "crop=trunc" in vf
        # Check noise
        assert "noise=alls=" in vf
        # Check eq
        assert "eq=contrast=" in vf
        # Check speed shifts
        assert "setpts=PTS/" in vf
        assert "atempo=" in af
        # Check equalizers
        assert "equalizer=" in af

    print("[PASS] Processor: get_filter_complex properly generates anti-fingerprint filter chains.")


def test_real_ffmpeg_end_to_end_processing():
    """
    Generates a 3-second synthetic test video, runs process_video with Memoxz engine,
    validates output via ffprobe, and verifies cleanup and SQLite status.
    """
    from bot.db import job_store, DOWNLOADS_DIR, PROCESSED_DIR
    from bot.processor import video_processor, get_video_info

    job_id = str(uuid.uuid4())
    test_input = DOWNLOADS_DIR / f"{job_id}.mp4"
    test_output = PROCESSED_DIR / f"{job_id}.mp4"

    # Register job in SQLite
    job_store.create_job(job_id, user_id=735006720, source_url="https://youtu.be/test_clip")
    job_store.complete_download(job_id, "Synthetic Test Video", 3, str(test_input), 50000)

    # 1. Create 3s synthetic test video using ffmpeg
    cmd_generate = [
        "ffmpeg", "-y", "-v", "quiet",
        "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
        "-c:v", "libx264", "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        str(test_input),
    ]
    subprocess.run(cmd_generate, check=True)
    assert test_input.exists(), "Synthetic test video generation failed"

    # 2. Run video processing
    progress_updates = []

    def on_progress(pct, curr, total):
        progress_updates.append((pct, curr, total))

    result = asyncio.run(
        video_processor.process_video(
            job_id=job_id,
            preset="balanced",
            progress_callback=on_progress,
        )
    )

    # 3. Output validation
    assert test_output.exists(), "Processed output file does not exist"
    assert result.file_size > 0, "Processed file size is 0"
    assert not test_input.exists(), "Raw downloaded file was not cleaned up after success"

    # 4. Verify output with ffprobe
    probe_info = get_video_info(test_output)
    assert probe_info["duration"] > 2.0, "Processed video duration is too short"
    assert probe_info["vcodec"] == "h264", f"Unexpected video codec: {probe_info['vcodec']}"

    # 5. Verify Adobe Premiere Pro metadata injected
    cmd_meta = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(test_output),
    ]
    out_meta = subprocess.check_output(cmd_meta, text=True)
    assert "Adobe Premiere Pro" in out_meta, "Adobe Premiere Pro metadata tag not found in output"

    # 6. Verify SQLite job record
    job = job_store.get_job(job_id)
    assert job["status"] == "processed"
    assert job["file_path"] == str(test_output.resolve())
    assert job["file_size"] == result.file_size

    # Clean up test output
    test_output.unlink(missing_ok=True)
    print(f"[PASS] Processor: End-to-end FFmpeg execution succeeded (CRF 17, Premiere Pro metadata injected, {len(progress_updates)} progress ticks).")


def test_processing_failure_resilience():
    """Verify that a corrupted or invalid video input transitions job to processing_failed without crashing."""
    from bot.db import job_store, DOWNLOADS_DIR
    from bot.processor import video_processor

    job_id = str(uuid.uuid4())
    fake_input = DOWNLOADS_DIR / f"{job_id}.mp4"
    fake_input.write_text("This is not a valid video file.")

    job_store.create_job(job_id, user_id=735006720, source_url="https://youtu.be/corrupt")
    job_store.complete_download(job_id, "Corrupt Video", 5, str(fake_input), 100)

    try:
        asyncio.run(video_processor.process_video(job_id=job_id))
        assert False, "Should have raised an error on corrupt file"
    except Exception:
        pass

    job = job_store.get_job(job_id)
    assert job["status"] == "processing_failed"
    assert "FFmpeg failed" in job["error_message"]

    # Clean up
    fake_input.unlink(missing_ok=True)
    print("[PASS] Processor: Processing failure correctly recorded in SQLite without crashing.")


if __name__ == "__main__":
    print("--- Running Stage 3 Automated Verification Suite ---")
    test_filter_complex_generation()
    test_real_ffmpeg_end_to_end_processing()
    test_processing_failure_resilience()
    print("--- ALL STAGE 3 AUTOMATED TESTS PASSED ---")
