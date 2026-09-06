"""Video processing engine using ffmpeg with Memoxz anti-fingerprint layer and Premiere Pro metadata."""

import asyncio
import json
import logging
import os
import random
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from bot.db import job_store

logger = logging.getLogger("bot.processor")

BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = BASE_DIR / "storage" / "downloads"
PROCESSED_DIR = BASE_DIR / "storage" / "processed"


@dataclass
class ProcessedResult:
    """Result object returned upon successful video processing."""
    job_id: str
    file_path: str
    file_size: int
    duration: float
    width: int
    height: int

    @property
    def output_path(self) -> str:
        """Convenience alias for file_path."""
        return self.file_path


def get_video_info(video_path: Path) -> Dict[str, Any]:
    """
    Extracts metadata, duration, streams, and dimensions using ffprobe.
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        data = json.loads(out)
        format_info = data.get("format", {})
        duration = float(format_info.get("duration", 0.0))

        width = 0
        height = 0
        vcodec = "unknown"
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width = int(stream.get("width", 0))
                height = int(stream.get("height", 0))
                vcodec = stream.get("codec_name", "unknown")
                break

        return {
            "duration": duration,
            "width": width,
            "height": height,
            "vcodec": vcodec,
            "size": int(format_info.get("size", 0)),
        }
    except Exception as exc:
        logger.error("Failed to inspect video %s with ffprobe: %s", video_path, exc)
        return {"duration": 0.0, "width": 0, "height": 0, "vcodec": "unknown", "size": 0}


def get_filter_complex(preset: str = "balanced") -> Tuple[str, str]:
    """
    Generates anti-fingerprint video and audio filter chains from Wel/main.py.
    Applies imperceptible micro-zoom, noise, color, speed, and acoustic EQ shifts.
    All dimensions are kept strictly even (trunc(.../2)*2) for maximal hardware encoding speed.
    """
    speed_factor = round(random.uniform(1.006, 1.014), 4)
    contrast_shift = round(random.uniform(1.01, 1.02), 3)
    brightness_shift = round(random.uniform(0.003, 0.008), 3)
    saturation_shift = round(random.uniform(1.01, 1.025), 3)
    gamma_shift = round(random.uniform(1.005, 1.015), 3)
    crop_factor = round(random.uniform(1.012, 1.020), 4)

    noise_flags = "allf=t+u"

    scale_crop_filters = [
        f"scale=trunc(iw*{crop_factor}/2)*2:trunc(ih*{crop_factor}/2)*2",
        f"crop=trunc(iw/2)*2:trunc(ih/2)*2",
    ]

    if preset == "subtle":
        video_filters = [
            *scale_crop_filters,
            f"noise=alls=0.6:{noise_flags}",
            f"eq=contrast={contrast_shift}:brightness={brightness_shift}:saturation={saturation_shift}",
            f"setpts=PTS/{speed_factor}",
        ]
        audio_filters = [
            f"atempo={speed_factor}",
            "equalizer=f=1000:t=q:w=1:g=0.3",
        ]
    elif preset == "aggressive":
        video_filters = [
            *scale_crop_filters,
            f"noise=alls=1.2:{noise_flags}",
            f"eq=contrast={contrast_shift}:brightness={brightness_shift}:saturation={saturation_shift}:gamma={gamma_shift}",
            "unsharp=3:3:0.2:3:3:0.0",
            f"setpts=PTS/{speed_factor}",
        ]
        audio_filters = [
            f"atempo={speed_factor}",
            "equalizer=f=80:t=q:w=1:g=0.3",
            "equalizer=f=1200:t=q:w=1:g=0.4",
            "equalizer=f=8000:t=q:w=1:g=0.2",
            "volume=1.01",
        ]
    else:
        # Default: "balanced" (maximum visual quality & hash breakup)
        video_filters = [
            *scale_crop_filters,
            f"noise=alls=0.8:{noise_flags}",
            f"eq=contrast={contrast_shift}:brightness={brightness_shift}:saturation={saturation_shift}:gamma={gamma_shift}",
            f"setpts=PTS/{speed_factor}",
        ]
        audio_filters = [
            f"atempo={speed_factor}",
            "equalizer=f=100:t=q:w=1:g=0.25",
            "equalizer=f=2000:t=q:w=1:g=0.3",
            "volume=1.008",
        ]

    # Ensure square pixels (SAR 1:1) strictly required by Instagram Reels specification
    video_filters.append("setsar=1")

    vf = ",".join(video_filters)
    af = ",".join(audio_filters)
    return vf, af


class VideoProcessor:
    """
    Executes the Memoxz studio-grade video processing pipeline using ffmpeg:
    - Anti-fingerprint visual and acoustic hash breakup
    - Stripping original metadata and injecting Adobe Premiere Pro 2024 tags
    - Master visual quality: CRF 17, 320k AAC, yuv420p, +faststart
    - Live progress streaming
    - ffprobe output verification and source cleanup
    """

    def __init__(self):
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    async def process_video(
        self,
        job_id: str,
        preset: str = "balanced",
        instructions: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable[[int, float, float], None]] = None,
        timeout_seconds: int = 300,
    ) -> ProcessedResult:
        """
        Processes the downloaded video into an Instagram-ready unique video in storage/processed/{job_id}.mp4.
        """
        input_path = DOWNLOADS_DIR / f"{job_id}.mp4"
        if not input_path.exists():
            # Look for any candidate
            candidates = list(DOWNLOADS_DIR.glob(f"{job_id}.*"))
            valid = [c for c in candidates if not c.name.endswith((".part", ".ytdl"))]
            if valid:
                input_path = valid[0]
            else:
                raise FileNotFoundError(f"Source video file not found for job {job_id}")

        output_path = PROCESSED_DIR / f"{job_id}.mp4"
        job_store.start_processing(job_id, instructions=instructions or {"preset": preset})

        # 1. Read source info for progress calculation
        src_info = get_video_info(input_path)
        total_duration = src_info.get("duration", 0.0)

        # 2. Build anti-fingerprint filter chains
        vf, af = get_filter_complex(preset=preset)

        # Optional user crop to 9:16 if requested
        if instructions and instructions.get("crop") == "9:16":
            vf = f"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,{vf}"

        # 3. Adobe Premiere Pro CC 2024 metadata
        current_utc = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())

        cmd = [
            "ffmpeg",
            "-nostdin",
            "-loglevel", "error",
            "-y",
            "-threads", "0",
            "-i", str(input_path),
            "-vf", vf,
            "-af", af,
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", "17",
            "-profile:v", "high",
            "-level", "4.2",
            "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
            "-color_range", "tv",
            "-c:a", "aac",
            "-b:a", "320k",
            "-ar", "48000",
            "-map_metadata", "-1",
            "-metadata", "title=",
            "-metadata", "artist=",
            "-metadata", "album=",
            "-metadata", f"creation_time={current_utc}",
            "-metadata", "encoder=Adobe Premiere Pro 2024 (Windows)",
            "-metadata", "software=Adobe Premiere Pro CC 24.2.1 (Windows)",
            "-metadata", "encoded_by=Adobe Media Encoder CC 2024",
            "-metadata", "comment=Rendered in Adobe Premiere Pro CC",
            "-metadata:s:v:0", "handler_name=VideoHandler",
            "-metadata:s:a:0", "handler_name=SoundHandler",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            str(output_path),
        ]

        logger.info("[%s] Launching FFmpeg with Memoxz engine (CRF 17, 320k AAC)...", job_id)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # 4. Stream progress and capture stderr without stream conflict
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())

        async def _read_progress():
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str.startswith("out_time_us="):
                    try:
                        us = int(line_str.split("=")[1])
                        curr_sec = us / 1_000_000.0
                        if total_duration > 0:
                            pct = min(99, max(0, int((curr_sec / total_duration) * 100)))
                            if progress_callback:
                                progress_callback(pct, curr_sec, total_duration)
                    except Exception:
                        pass

        try:
            await asyncio.wait_for(
                asyncio.gather(_read_progress(), process.wait()),
                timeout=timeout_seconds,
            )
            stderr_bytes = await stderr_task
        except asyncio.TimeoutError:
            logger.error("[%s] FFmpeg process timed out after %d seconds. Terminating...", job_id, timeout_seconds)
            try:
                process.kill()
            except Exception:
                pass
            job_store.fail_processing(job_id, f"Processing timed out after {timeout_seconds}s")
            raise TimeoutError(f"Video processing timed out after {timeout_seconds} seconds.")

        if process.returncode != 0:
            err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
            logger.error("[%s] FFmpeg processing failed with code %d:\n%s", job_id, process.returncode, err_msg)
            job_store.fail_processing(job_id, f"FFmpeg failed: {err_msg[:200]}")
            # Clean up corrupted output if any
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            raise RuntimeError(f"FFmpeg processing failed: {err_msg[:200]}")

        # 5. Output Validation via ffprobe
        if not output_path.exists() or output_path.stat().st_size == 0:
            job_store.fail_processing(job_id, "Output video file missing or 0 bytes.")
            raise RuntimeError("Processed video file is empty or missing.")

        out_info = get_video_info(output_path)
        out_duration = out_info.get("duration", 0.0)
        out_size = output_path.stat().st_size

        logger.info(
            "[%s] Video processing complete. Output size: %d bytes, duration: %.2fs",
            job_id,
            out_size,
            out_duration,
        )

        # 6. Mark completed in job store
        job_store.complete_processing(
            job_id=job_id,
            processed_file_path=str(output_path.resolve()),
            file_size=out_size,
            duration=out_duration,
        )

        # 7. Clean up original download to save disk space
        try:
            if input_path.exists():
                input_path.unlink(missing_ok=True)
                logger.info("[%s] Cleaned up temporary raw download: %s", job_id, input_path.name)
        except Exception as e:
            logger.warning("[%s] Failed to remove temporary raw download: %s", job_id, e)

        return ProcessedResult(
            job_id=job_id,
            file_path=str(output_path.resolve()),
            file_size=out_size,
            duration=out_duration,
            width=out_info.get("width", 0),
            height=out_info.get("height", 0),
        )


# Global singleton instance
video_processor = VideoProcessor()
