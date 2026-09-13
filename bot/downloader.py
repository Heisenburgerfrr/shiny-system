"""YouTube video downloader using yt-dlp with Cloud IP workarounds, retries, and error classification."""

import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

from bot.config import config
from bot.db import job_store

logger = logging.getLogger("bot.downloader")

BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = BASE_DIR / "storage" / "downloads"


@dataclass
class DownloadResult:
    """Result returned upon successful download."""
    job_id: str
    title: str
    duration: Optional[int]
    file_path: str
    file_size: int
    is_carousel: bool = False
    carousel_items: Optional[List[Dict[str, Any]]] = None


class DownloadCategory:
    """Error categories for plain-language user reporting."""
    UNAVAILABLE_OR_PRIVATE = "UNAVAILABLE_OR_PRIVATE"
    GEO_BLOCKED = "GEO_BLOCKED"
    BOT_DETECTED = "BOT_DETECTED"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    UNKNOWN = "UNKNOWN"


def classify_error(exc: Exception) -> Tuple[str, str]:
    """
    Classifies yt-dlp exceptions into user-friendly categories and plain-language messages.
    Returns (category, user_friendly_message).
    """
    err_str = str(exc).lower()

    # Bot detection / Sign-in required
    if any(k in err_str for k in [
        "sign in to confirm",
        "confirm you're not a bot",
        "bot verification",
        "automated queries",
        "captcha",
        "the page needs to be reloaded",
        "please sign in",
        "no video formats found",
        "no longer supported in this application",
        "login required",
    ]):
        return (
            DownloadCategory.BOT_DETECTED,
            "YouTube is requesting bot verification or sign-in for this server IP. "
            "A browser cookies file (YTDLP_COOKIES_PATH) is recommended to bypass this on datacenter IPs.",
        )

    # Geo-blocked (checked before generic unavailable)
    if any(k in err_str for k in [
        "not available in your country",
        "geo-restricted",
        "geoblocked",
        "blocked in your country",
        "uploader has not made this video available in your country",
    ]):
        return (
            DownloadCategory.GEO_BLOCKED,
            "This video is restricted or geo-blocked in the server's region.",
        )

    # Format errors should not be treated as private/unavailable videos
    if "requested format is not available" in err_str or "no video formats found" in err_str:
        return (
            DownloadCategory.UNKNOWN,
            "The requested video format was not available for the selected client.",
        )

    # Private or deleted or unavailable
    if any(k in err_str for k in [
        "private video",
        "video is unavailable",
        "this video has been removed",
        "has been terminated",
        "does not exist",
        "video is not available",
        "uploader has closed their youtube account",
    ]):
        return (
            DownloadCategory.UNAVAILABLE_OR_PRIVATE,
            "This video is private, removed, or unavailable.",
        )

    # Network timeout or connection error
    if any(k in err_str for k in [
        "timed out",
        "timeout",
        "connection reset",
        "network is unreachable",
        "connection refused",
        "temporary failure in name resolution",
    ]):
        return (
            DownloadCategory.NETWORK_TIMEOUT,
            "Connection timed out or network error occurred while connecting to YouTube.",
        )

    # Generic / Unknown
    return (
        DownloadCategory.UNKNOWN,
        f"Download failed due to an unexpected error: {str(exc)[:150]}",
    )


def is_permanent_failure(category: str) -> bool:
    """Returns True if retrying the exact same request would never succeed."""
    return category in (
        DownloadCategory.UNAVAILABLE_OR_PRIVATE,
        DownloadCategory.GEO_BLOCKED,
    )


def _format_bytes(bytes_count: Optional[float]) -> str:
    """Formats byte counts into human-readable strings."""
    if not bytes_count:
        return "N/A"
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_count < 1024.0:
            return f"{bytes_count:.1f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.1f} TB"


def _format_seconds(seconds: Optional[float]) -> str:
    """Formats seconds into MM:SS format."""
    if seconds is None:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class YouTubeDownloader:
    """
    Downloads YouTube videos using yt-dlp with:
    - Target output named by job_id
    - Cookie file verification (falls back with warning if invalid)
    - Player client fallback rotation (android -> ios -> web)
    - Retry logic for transient network failures
    - Live progress hook for Telegram status messages
    - Automatic partial-file cleanup on failure
    """

    def __init__(self):
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    def _get_validated_cookies_path(self) -> Optional[str]:
        """Validates cookies path; falls back to cookies.txt in project root."""
        candidate = None
        if config.ytdlp_cookies_path:
            path = Path(config.ytdlp_cookies_path)
            if not path.is_absolute():
                path = BASE_DIR / path
            if path.is_file():
                candidate = path
            else:
                logger.warning(
                    "YTDLP_COOKIES_PATH is configured ('%s') but file was not found.",
                    config.ytdlp_cookies_path,
                )
        if not candidate:
            default_cookies = BASE_DIR / "cookies.txt"
            if default_cookies.is_file():
                candidate = default_cookies

        if candidate:
            logger.info("Using YouTube cookies: %s", candidate.resolve())
            return str(candidate.resolve())
        return None

    def _build_ydl_opts(
        self,
        job_id: str,
        player_client: str,
        progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
        is_instagram: bool = False,
    ) -> Dict[str, Any]:
        """Constructs yt-dlp options dictionary for YouTube or Instagram."""
        if is_instagram:
            job_dir = DOWNLOADS_DIR / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            output_template = str(job_dir / "%(autonumber)02d_%(id)s.%(ext)s")
        else:
            output_template = str(DOWNLOADS_DIR / f"{job_id}.%(ext)s")

        cookies_path = self._get_validated_cookies_path()

        if is_instagram:
            format_str = "bestvideo*+bestaudio/best"
            format_sort = ["res:1080", "quality", "size", "br", "fps"]
        else:
            # For YouTube: Strictly prioritize original audio and English audio tracks over foreign dubs
            format_str = (
                "bestvideo*+("
                "bestaudio[language_preference>=10]/"
                "bestaudio[format_note*=original]/"
                "bestaudio[language^=en]/"
                "bestaudio[language=en]/"
                "bestaudio"
                ")/best"
            )
            format_sort = ["res:1080", "lang:en", "quality", "size", "br", "fps"]

        ydl_opts: Dict[str, Any] = {
            "format": format_str,
            "format_sort": format_sort,
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "socket_timeout": 30,
            "quiet": False,
            "no_warnings": False,
            "nocheckcertificate": False,
        }

        if not is_instagram:
            ydl_opts["remote_components"] = ["ejs:github"]

            # Explicitly configure Deno runtime path for yt-dlp JS challenge solving
            deno_bin = shutil.which("deno")
            if not deno_bin:
                for candidate in [
                    Path.home() / ".deno" / "bin" / "deno",
                    Path("/usr/local/bin/deno"),
                    Path("/usr/bin/deno"),
                ]:
                    if candidate.is_file():
                        deno_bin = str(candidate)
                        break

            if deno_bin:
                ydl_opts["js_runtimes"] = {"deno": {"path": deno_bin}}
                logger.info("[%s] Using Deno JS runtime at: %s", job_id, deno_bin)

            youtube_args: Dict[str, Any] = {
                "lang": ["en"],
            }
            if player_client and player_client.lower() != "default":
                youtube_args["player_client"] = [player_client]

            ydl_opts["extractor_args"] = {"youtube": youtube_args}

        if cookies_path:
            ydl_opts["cookiefile"] = cookies_path

        if config.ytdlp_proxy_url:
            ydl_opts["proxy"] = config.ytdlp_proxy_url

        if progress_hook:
            ydl_opts["progress_hooks"] = [progress_hook]

        return ydl_opts

    def _download_sync(
        self,
        job_id: str,
        url: str,
        progress_callback: Optional[Callable[[float, str, str], None]] = None,
    ) -> DownloadResult:
        """
        Synchronous download execution with player client fallback and network retries.
        Runs inside asyncio.to_thread to keep the event loop non-blocking.
        """
        player_clients: List[str] = list(config.ytdlp_player_clients)
        if "default" in player_clients:
            player_clients.remove("default")
        player_clients.insert(0, "default")
        max_network_retries = 3

        last_exception: Optional[Exception] = None
        last_category: str = DownloadCategory.UNKNOWN
        last_user_message: str = "Download failed."

        def ytdlp_hook(data: Dict[str, Any]) -> None:
            if not progress_callback:
                return
            status = data.get("status")
            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                downloaded = data.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100.0) if total > 0 else 0.0
                speed = data.get("speed")
                eta = data.get("eta")

                speed_str = f"{_format_bytes(speed)}/s" if speed else "N/A"
                eta_str = _format_seconds(eta)
                try:
                    progress_callback(percent, speed_str, eta_str)
                except Exception:
                    pass

        # 1. Instagram Download Flow (Reels & Carousels)
        if "instagram.com" in url.lower():
            logger.info("[%s] Downloading Instagram media: %s", job_id, url)
            for attempt in range(1, max_network_retries + 1):
                try:
                    opts = self._build_ydl_opts(job_id, "default", ytdlp_hook, is_instagram=True)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        if not info:
                            raise DownloadError("Failed to extract Instagram media.")

                        raw_title = info.get("title") or info.get("description") or "Instagram Media"
                        title = raw_title.replace("\n", " ").strip()
                        if len(title) > 60:
                            title = title[:57] + "..."
                        duration = info.get("duration")

                        job_dir = DOWNLOADS_DIR / job_id
                        found_files = []
                        if job_dir.exists():
                            for f in sorted(job_dir.glob("*")):
                                if f.is_file() and not f.name.endswith((".part", ".ytdl")):
                                    found_files.append(f)
                        if not found_files:
                            for f in sorted(DOWNLOADS_DIR.glob(f"{job_id}*")):
                                if f.is_file() and not f.name.endswith((".part", ".ytdl")):
                                    found_files.append(f)

                        if not found_files:
                            raise FileNotFoundError(f"No downloaded media found for Instagram job {job_id}")

                        VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
                        IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

                        # Case A: Single video or image post
                        if len(found_files) == 1:
                            single_file = found_files[0]
                            # Move to DOWNLOADS_DIR / f"{job_id}{single_file.suffix}" for standard flat file compatibility
                            dest_file = DOWNLOADS_DIR / f"{job_id}{single_file.suffix}"
                            try:
                                shutil.move(str(single_file), str(dest_file))
                                shutil.rmtree(job_dir, ignore_errors=True)
                            except Exception as move_err:
                                logger.warning("[%s] Failed to move single Instagram file to root: %s", job_id, move_err)
                                dest_file = single_file

                            file_size = dest_file.stat().st_size
                            return DownloadResult(
                                job_id=job_id,
                                title=title,
                                duration=duration,
                                file_path=str(dest_file.resolve()),
                                file_size=file_size,
                                is_carousel=False,
                            )

                        # Case B: Multi-item Carousel post
                        items = []
                        total_size = 0
                        for f in found_files:
                            ext = f.suffix.lower()
                            item_type = "video" if ext in VIDEO_EXTS else "image" if ext in IMAGE_EXTS else "video"
                            sz = f.stat().st_size
                            total_size += sz
                            items.append({
                                "type": item_type,
                                "file_path": str(f.resolve()),
                                "file_size": sz,
                            })

                        logger.info(
                            "[%s] Instagram carousel download complete: %d items (%d videos, %d images)",
                            job_id,
                            len(items),
                            sum(1 for i in items if i["type"] == "video"),
                            sum(1 for i in items if i["type"] == "image"),
                        )
                        return DownloadResult(
                            job_id=job_id,
                            title=title,
                            duration=duration,
                            file_path=str(found_files[0].resolve()),
                            file_size=total_size,
                            is_carousel=True,
                            carousel_items=items,
                        )
                except Exception as exc:
                    last_exception = exc
                    category, user_msg = classify_error(exc)
                    last_category = category
                    last_user_message = user_msg
                    logger.warning(
                        "[%s] Instagram download attempt %d/%d failed: %s",
                        job_id,
                        attempt,
                        max_network_retries,
                        exc,
                    )
                    if attempt < max_network_retries:
                        time.sleep(2 ** attempt)

            self._cleanup_partial_files(job_id)
            raise RuntimeError(f"[{last_category}] {last_user_message}") from last_exception

        # 2. YouTube Download Flow (with player client fallback rotation)
        for client_idx, client in enumerate(player_clients):
            logger.info(
                "[%s] Attempting download with player_client='%s' (option %d of %d)...",
                job_id,
                client,
                client_idx + 1,
                len(player_clients),
            )

            # Retry loop for transient network issues
            for attempt in range(1, max_network_retries + 1):
                try:
                    opts = self._build_ydl_opts(job_id, client, ytdlp_hook)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        if not info:
                            raise DownloadError("Failed to extract video information.")

                        title = info.get("title", "Untitled Video")
                        duration = info.get("duration")
                        expected_file = DOWNLOADS_DIR / f"{job_id}.mp4"

                        if not expected_file.exists():
                            # Look for any file matching job_id.mp4 or other extension
                            candidates = list(DOWNLOADS_DIR.glob(f"{job_id}.*"))
                            valid_candidates = [
                                c for c in candidates if not c.name.endswith((".part", ".ytdl"))
                            ]
                            if valid_candidates:
                                expected_file = valid_candidates[0]
                            else:
                                raise FileNotFoundError(f"Downloaded file not found for job {job_id}")

                        file_size = expected_file.stat().st_size
                        logger.info(
                            "[%s] Download completed successfully: '%s' (%s, %d bytes)",
                            job_id,
                            title,
                            expected_file.name,
                            file_size,
                        )
                        return DownloadResult(
                            job_id=job_id,
                            title=title,
                            duration=duration,
                            file_path=str(expected_file.resolve()),
                            file_size=file_size,
                        )

                except Exception as exc:
                    last_exception = exc
                    category, user_msg = classify_error(exc)
                    last_category = category
                    last_user_message = user_msg

                    logger.warning(
                        "[%s] Download error on player_client='%s' (attempt %d/%d): category=%s, details=%s",
                        job_id,
                        client,
                        attempt,
                        max_network_retries,
                        category,
                        exc,
                    )

                    # 1. If permanent failure (private/deleted/geoblocked), abort immediately
                    if is_permanent_failure(category):
                        self._cleanup_partial_files(job_id)
                        raise RuntimeError(f"[{category}] {user_msg}") from exc

                    # 2. If bot detected, break out to try the next player client immediately
                    if category == DownloadCategory.BOT_DETECTED:
                        break

                    # 3. If transient network issue, retry with exponential backoff
                    if attempt < max_network_retries:
                        backoff = 2 ** attempt
                        logger.info("[%s] Retrying in %ds...", job_id, backoff)
                        time.sleep(backoff)

        # If all player clients and retries were exhausted
        self._cleanup_partial_files(job_id)
        final_msg = f"[{last_category}] {last_user_message}"
        raise RuntimeError(final_msg) from last_exception

    def _cleanup_partial_files(self, job_id: str) -> None:
        """Cleans up any partial or incomplete files left on disk for this job."""
        if not DOWNLOADS_DIR.exists():
            return
        job_dir = DOWNLOADS_DIR / job_id
        if job_dir.is_dir():
            shutil.rmtree(job_dir, ignore_errors=True)
            logger.debug("[%s] Cleaned up job download directory: %s", job_id, job_dir.name)
        for p in DOWNLOADS_DIR.glob(f"{job_id}*"):
            try:
                if p.is_file():
                    p.unlink(missing_ok=True)
                    logger.debug("[%s] Cleaned up partial file: %s", job_id, p.name)
            except Exception as e:
                logger.warning("[%s] Failed to clean up file %s: %s", job_id, p.name, e)

    async def download(
        self,
        job_id: str,
        url: str,
        progress_callback: Optional[Callable[[float, str, str], None]] = None,
    ) -> DownloadResult:
        """
        Asynchronously downloads a YouTube video to storage/downloads/{job_id}.mp4.
        Updates job store on success or failure.
        """
        job_store.update_status(job_id, "downloading")
        try:
            result = await asyncio.to_thread(
                self._download_sync, job_id, url, progress_callback
            )
            job_store.complete_download(
                job_id=job_id,
                title=result.title,
                duration=result.duration,
                file_path=result.file_path,
                file_size=result.file_size,
            )
            return result
        except Exception as exc:
            err_text = str(exc)
            # Remove category bracket for clean database error message
            clean_err = re.sub(r"^\[[A-Z_]+\]\s*", "", err_text)
            job_store.update_status(job_id, "failed", error_message=clean_err)
            raise


# Singleton downloader instance
downloader = YouTubeDownloader()
