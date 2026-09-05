"""System state recovery, local file cleanup, task cancellation, and resilience tracking."""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from bot.config import config
from bot.db import DOWNLOADS_DIR, PROCESSED_DIR, STORAGE_DIR, job_store

logger = logging.getLogger("bot.recovery")

TEMP_DIR = STORAGE_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SWEEP_MAX_AGE_HOURS = 48
SYSTEMIC_FAILURE_THRESHOLD = 3


class TaskRegistry:
    """Tracks running asyncio.Task instances for active jobs, enabling manual cancellation."""

    def __init__(self):
        self._tasks: Dict[str, asyncio.Task] = {}

    def register_task(self, job_id: str, task: asyncio.Task) -> None:
        """Associates an in-flight background task with a job_id."""
        self._tasks[job_id] = task
        logger.debug("[%s] Registered background task in TaskRegistry.", job_id)

    def unregister_task(self, job_id: str) -> None:
        """Removes a finished task from the registry."""
        self._tasks.pop(job_id, None)

    def cancel_task(self, job_id: str) -> bool:
        """
        Cancels the active background task for a given job if running.
        Returns True if a task was found and cancellation was requested.
        """
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            self.unregister_task(job_id)
            logger.info("[%s] In-flight background task successfully cancelled.", job_id)
            return True
        return False

    def is_task_running(self, job_id: str) -> bool:
        """Checks if a background task is currently active for a job_id."""
        task = self._tasks.get(job_id)
        return bool(task and not task.done())


class StorageCleaner:
    """Manages immediate per-job local artifact deletion and periodic safety net sweeps."""

    @staticmethod
    def cleanup_job_local_files(job_id: str) -> List[Path]:
        """
        Deletes all local temporary files associated with job_id from downloads,
        processed, and temp folders. Returns list of deleted file paths.
        """
        deleted: List[Path] = []
        directories = [DOWNLOADS_DIR, PROCESSED_DIR, TEMP_DIR]

        for d in directories:
            if not d.exists():
                continue
            for file_path in d.glob(f"{job_id}*"):
                try:
                    if file_path.is_file():
                        file_path.unlink(missing_ok=True)
                        deleted.append(file_path)
                        logger.debug("[%s] Deleted local artifact: %s", job_id, file_path.name)
                except Exception as exc:
                    logger.warning("[%s] Failed to delete artifact %s: %s", job_id, file_path.name, exc)

        if deleted:
            logger.info("[%s] Local storage cleanup complete. Removed %d file(s).", job_id, len(deleted))
        return deleted

    @staticmethod
    def periodic_storage_sweep(max_age_hours: int = DEFAULT_SWEEP_MAX_AGE_HOURS) -> List[Path]:
        """
        Safety net retention sweep: deletes files older than max_age_hours that do not
        belong to an active, in-progress job.
        """
        now = time.time()
        max_age_seconds = max_age_hours * 3600
        deleted: List[Path] = []

        # Get set of active job IDs to prevent deleting in-flight data
        active_jobs = job_store.list_in_progress_jobs()
        active_job_ids: Set[str] = {j["job_id"] for j in active_jobs}

        directories = [DOWNLOADS_DIR, PROCESSED_DIR, TEMP_DIR]
        for d in directories:
            if not d.exists():
                continue
            for file_path in d.glob("*.*"):
                if not file_path.is_file():
                    continue

                # Check if file belongs to an active job
                file_stem = file_path.stem
                if any(jid in file_stem for jid in active_job_ids):
                    continue

                try:
                    file_age = now - file_path.stat().st_mtime
                    if file_age > max_age_seconds:
                        file_path.unlink(missing_ok=True)
                        deleted.append(file_path)
                        logger.info(
                            "Periodic sweep removed orphaned file: %s (age: %.1f hours)",
                            file_path.name,
                            file_age / 3600,
                        )
                except Exception as exc:
                    logger.warning("Failed to sweep file %s: %s", file_path.name, exc)

        return deleted


class SystemicFailureTracker:
    """Tracks failure frequencies across jobs to identify systemic/external outages."""

    def __init__(self, threshold: int = SYSTEMIC_FAILURE_THRESHOLD):
        self.threshold = threshold
        self._consecutive_failures: Dict[str, int] = {
            "download": 0,
            "processing": 0,
            "azure_upload": 0,
            "instagram_publish": 0,
            "token": 0,
        }

    def record_failure(self, failure_type: str, job_id: str, detail: str = "") -> Optional[str]:
        """
        Records a failure for a given pipeline stage.
        If consecutive failures reach threshold (or on token expiration), returns an alert message.
        """
        normalized_type = failure_type.lower()
        count = self._consecutive_failures.get(normalized_type, 0) + 1
        self._consecutive_failures[normalized_type] = count

        logger.warning(
            "[%s] Recorded failure for '%s' (consecutive count: %d)",
            job_id,
            normalized_type,
            count,
        )

        # Immediate alert for token failure
        if normalized_type == "token":
            return (
                "🚨 **CRITICAL SYSTEMIC ALERT: Instagram Token Expired**\n\n"
                "Meta rejected the Instagram access token. All upcoming Reel posts will fail.\n"
                "⚠️ **Action Required**: Generate a new 60-day token and update `INSTAGRAM_ACCESS_TOKEN`."
            )

        # Alert if threshold reached
        if count >= self.threshold:
            # Reset after alerting to avoid spamming every consecutive failure
            self._consecutive_failures[normalized_type] = 0
            return (
                f"🚨 **SYSTEMIC ALERT: Recurring {normalized_type.replace('_', ' ').title()} Failures**\n\n"
                f"• Detected **{count} consecutive failures** in the `{normalized_type}` stage.\n"
                f"• Last Failed Job: `{job_id}`\n"
                f"• Error Context: {detail[:120] if detail else 'Check logs for details.'}\n\n"
                f"⚠️ This may indicate an external service disruption (e.g. YouTube bot detection, Azure storage limits, or network connectivity)."
            )

        return None

    def record_success(self, failure_type: str) -> None:
        """Resets the consecutive failure counter for a stage upon success."""
        normalized_type = failure_type.lower()
        if normalized_type in self._consecutive_failures:
            self._consecutive_failures[normalized_type] = 0


class StartupRecoveryManager:
    """Scans and recovers jobs left in non-terminal states across bot restarts."""

    @staticmethod
    def scan_and_recover_jobs() -> List[Dict[str, Any]]:
        """
        Scans SQLite for interrupted jobs.
        Applies safety rules:
        - Interrupted 'publishing' jobs are flagged for manual review to avoid duplicate posts.
        - Stages before publishing are transitioned to clear failure states and cleaned up.
        """
        interrupted = job_store.list_in_progress_jobs()
        recovered_summaries: List[Dict[str, Any]] = []

        now = datetime.now(timezone.utc).isoformat()
        for job in interrupted:
            jid = job["job_id"]
            prev_status = job["status"]
            source_url = job["source_url"]

            summary = {
                "job_id": jid,
                "previous_status": prev_status,
                "source_url": source_url,
                "title": job.get("title") or "Unknown",
                "needs_manual_review": False,
            }

            if prev_status == "publishing":
                # Do NOT auto-resume publishing: container might already be live!
                err = (
                    "Interrupted during publishing by bot restart. "
                    "Check Instagram manually before retrying to prevent duplicate posts."
                )
                job_store.fail_publishing(jid, err)
                summary["needs_manual_review"] = True
                summary["action_taken"] = "Marked publish_failed (review required)"

            elif prev_status in ("pending", "downloading"):
                err = "Interrupted during download by bot restart."
                job_store.update_status(jid, "download_failed", err)
                StorageCleaner.cleanup_job_local_files(jid)
                summary["action_taken"] = "Marked download_failed & cleaned local files"

            elif prev_status == "processing":
                err = "Interrupted during video processing by bot restart."
                job_store.fail_processing(jid, err)
                StorageCleaner.cleanup_job_local_files(jid)
                summary["action_taken"] = "Marked processing_failed & cleaned local files"

            elif prev_status == "uploading_to_azure":
                err = "Interrupted during Azure upload by bot restart."
                job_store.fail_azure_upload(jid, err)
                summary["action_taken"] = "Marked azure_upload_failed"

            elif prev_status == "awaiting_caption":
                # Safe to keep awaiting caption
                summary["action_taken"] = "Retained in awaiting_caption state"

            else:
                job_store.update_status(jid, "failed", "Interrupted by bot restart")
                summary["action_taken"] = f"Marked failed from {prev_status}"

            recovered_summaries.append(summary)
            logger.warning(
                "[%s] Startup recovery: previous_status='%s', action='%s'",
                jid,
                prev_status,
                summary["action_taken"],
            )

        return recovered_summaries

    @staticmethod
    def format_recovery_report(recovered_jobs: List[Dict[str, Any]]) -> Optional[str]:
        """Formats the list of recovered jobs into a clear Telegram notification."""
        if not recovered_jobs:
            return None

        lines = [
            f"🔄 **Bot Startup Recovery Report**\n",
            f"Recovered `{len(recovered_jobs)}` interrupted job(s) from previous session:\n",
        ]

        for item in recovered_jobs:
            warning_icon = "⚠️" if item.get("needs_manual_review") else "•"
            lines.append(
                f"{warning_icon} **Job ID**: `{item['job_id']}`\n"
                f"  Status: `{item['previous_status']}` → {item['action_taken']}\n"
                f"  URL: {item['source_url'][:60]}"
            )

        lines.append("\nUse `/jobs` to view active tasks or `/retry <job_id>` to resume.")
        return "\n".join(lines)


# Global instances
task_registry = TaskRegistry()
storage_cleaner = StorageCleaner()
failure_tracker = SystemicFailureTracker()
startup_recovery = StartupRecoveryManager()
