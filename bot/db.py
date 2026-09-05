"""SQLite job store for tracking download, processing, and publishing pipelines."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
DB_PATH = STORAGE_DIR / "jobs.db"
DOWNLOADS_DIR = STORAGE_DIR / "downloads"
PROCESSED_DIR = STORAGE_DIR / "processed"


def _utc_now_iso() -> str:
    """Returns current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """Manages persistent job state in SQLite."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that guarantees SQLite connections are properly closed."""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Creates the jobs table, adds new columns if needed, and sets indexes."""
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    source_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT,
                    duration INTEGER,
                    file_path TEXT,
                    file_size INTEGER,
                    error_message TEXT,
                    processing_instructions TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Check for processing_instructions and Stage 5 columns in existing DBs
            cursor = conn.execute("PRAGMA table_info(jobs)")
            columns = [row["name"] for row in cursor.fetchall()]
            if "processing_instructions" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN processing_instructions TEXT")
            if "video_blob_name" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN video_blob_name TEXT")
            if "video_sas_url" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN video_sas_url TEXT")
            if "video_sas_expires_at" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN video_sas_expires_at TEXT")
            if "caption" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN caption TEXT")
            if "instagram_container_id" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN instagram_container_id TEXT")
            if "instagram_media_id" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN instagram_media_id TEXT")
            if "instagram_permalink" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN instagram_permalink TEXT")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id)"
            )
            conn.commit()

    def create_job(
        self,
        job_id: str,
        user_id: int,
        source_url: str,
        caption: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Registers a new job in 'pending' status, optionally storing pre-supplied caption."""
        now = _utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, user_id, source_url, status, caption, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                (job_id, user_id, source_url, caption, now, now),
            )
            conn.commit()
        return self.get_job(job_id)  # type: ignore

    def update_status(
        self,
        job_id: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        """Updates the status and error_message of a job."""
        now = _utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (status, error_message, now, job_id),
            )
            conn.commit()

    def complete_download(
        self,
        job_id: str,
        title: str,
        duration: Optional[int],
        file_path: str,
        file_size: int,
    ) -> None:
        """Marks a job as successfully 'downloaded' with extracted metadata."""
        now = _utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'downloaded',
                    title = ?,
                    duration = ?,
                    file_path = ?,
                    file_size = ?,
                    error_message = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (title, duration, file_path, file_size, now, job_id),
            )
            conn.commit()

    def start_processing(
        self,
        job_id: str,
        instructions: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Transitions job to 'processing' status with saved instructions."""
        now = _utc_now_iso()
        instr_str = json.dumps(instructions or {})
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'processing',
                    processing_instructions = ?,
                    error_message = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (instr_str, now, job_id),
            )
            conn.commit()

    def complete_processing(
        self,
        job_id: str,
        processed_file_path: str,
        file_size: int,
        duration: Optional[float] = None,
    ) -> None:
        """Marks a job as successfully 'processed' with output file specs."""
        now = _utc_now_iso()
        dur_int = int(duration) if duration is not None else None
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'processed',
                    file_path = ?,
                    file_size = ?,
                    duration = COALESCE(?, duration),
                    error_message = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (processed_file_path, file_size, dur_int, now, job_id),
            )
            conn.commit()

    def fail_processing(self, job_id: str, error_message: str) -> None:
        """Marks a job as 'processing_failed' with detailed error."""
        now = _utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'processing_failed',
                    error_message = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (error_message, now, job_id),
            )
            conn.commit()

    def start_azure_upload(self, job_id: str) -> bool:
        """Transitions job status to 'uploading_to_azure'."""
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'uploading_to_azure', updated_at = ?
                WHERE job_id = ?
                """,
                (now, job_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def complete_azure_upload(
        self,
        job_id: str,
        blob_name: str,
        sas_url: str,
        expires_at: str,
    ) -> bool:
        """Transitions job status to 'hosted' and stores public SAS access information."""
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'hosted',
                    video_blob_name = ?,
                    video_sas_url = ?,
                    video_sas_expires_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (blob_name, sas_url, expires_at, now, job_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def fail_azure_upload(self, job_id: str, error_message: str) -> bool:
        """Transitions job status to 'azure_upload_failed' with error explanation."""
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'azure_upload_failed',
                    error_message = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (error_message, now, job_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    # =====================================================================
    # Stage 6: Instagram Publishing Methods
    # =====================================================================

    def set_awaiting_caption(self, job_id: str) -> bool:
        """Transitions job status to 'awaiting_caption'."""
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'awaiting_caption', updated_at = ?
                WHERE job_id = ?
                """,
                (now, job_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def set_job_caption(self, job_id: str, caption: str) -> bool:
        """Updates the caption on a job without altering its status."""
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET caption = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (caption, now, job_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_active_awaiting_caption_job(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Finds the most recent job awaiting caption for a specific user."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM jobs
                WHERE user_id = ? AND status = 'awaiting_caption'
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

    def start_publishing(self, job_id: str, caption: Optional[str] = None) -> bool:
        """Transitions job to 'publishing' status, recording caption if specified."""
        now = _utc_now_iso()
        with self._connection() as conn:
            if caption is not None:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'publishing', caption = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (caption, now, job_id),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'publishing', updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now, job_id),
                )
            conn.commit()
            return cursor.rowcount > 0

    def record_container_created(self, job_id: str, container_id: str) -> bool:
        """Stores the Instagram media container ID for tracking and polling."""
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET instagram_container_id = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (container_id, now, job_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def complete_publishing(self, job_id: str, media_id: str, permalink: str) -> bool:
        """Marks job as 'published' and stores final media ID and public post permalink."""
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'published',
                    instagram_media_id = ?,
                    instagram_permalink = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (media_id, permalink, now, job_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def fail_publishing(self, job_id: str, error_message: str) -> bool:
        """Marks job as 'publish_failed' with error explanation."""
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'publish_failed',
                    error_message = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (error_message, now, job_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def count_published_in_last_24h(self) -> int:
        """Counts how many Reels were published in the rolling last 24 hours."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT COUNT(*) FROM jobs
                WHERE status = 'published' AND updated_at >= ?
                """,
                (cutoff,),
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single job by its UUID."""
        with self._connection() as conn:
            cursor = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()
            if row:
                d = dict(row)
                if d.get("processing_instructions"):
                    try:
                        d["processing_instructions"] = json.loads(d["processing_instructions"])
                    except Exception:
                        pass
                return d
            return None

    def list_jobs_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Returns all jobs matching a given status."""
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
            return [dict(r) for r in cursor.fetchall()]

    def _cleanup_files_for_job(self, job_id: str) -> None:
        """Helper to remove raw and processed local files for a job."""
        for d in (DOWNLOADS_DIR, PROCESSED_DIR, STORAGE_DIR / "temp"):
            if d.exists():
                for f in d.glob(f"{job_id}*"):
                    try:
                        if f.is_file():
                            f.unlink(missing_ok=True)
                    except Exception:
                        pass

    def recover_interrupted_jobs(self) -> List[str]:
        """
        Recovers jobs left in 'pending', 'downloading', 'processing', 'uploading_to_azure', or 'publishing' states on bot restart.
        Marks them as 'failed', 'processing_failed', 'azure_upload_failed', or 'publish_failed'.
        Returns list of recovered job IDs.
        """
        recovered_ids = []
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT job_id, status FROM jobs WHERE status IN ('pending', 'downloading', 'processing', 'uploading_to_azure', 'publishing')"
            )
            rows = cursor.fetchall()
            for row in rows:
                jid = row["job_id"]
                st = row["status"]
                recovered_ids.append(jid)
                if st in ("pending", "downloading"):
                    self._cleanup_files_for_job(jid)

            if recovered_ids:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = CASE
                            WHEN status = 'processing' THEN 'processing_failed'
                            WHEN status = 'uploading_to_azure' THEN 'azure_upload_failed'
                            WHEN status = 'publishing' THEN 'publish_failed'
                            ELSE 'failed'
                        END,
                        error_message = 'Interrupted by bot restart',
                        updated_at = ?
                    WHERE status IN ('pending', 'downloading', 'processing', 'uploading_to_azure', 'publishing')
                    """,
                    (now,),
                )
                conn.commit()
        return recovered_ids

    def list_in_progress_jobs(self) -> List[Dict[str, Any]]:
        """Returns all jobs currently in progress (not in a terminal state)."""
        terminal_states = (
            "published",
            "cancelled",
            "failed",
            "download_failed",
            "processing_failed",
            "azure_upload_failed",
            "publish_failed",
        )
        placeholders = ",".join("?" for _ in terminal_states)
        with self._connection() as conn:
            cursor = conn.execute(
                f"SELECT * FROM jobs WHERE status NOT IN ({placeholders}) ORDER BY created_at DESC",
                terminal_states,
            )
            return [dict(r) for r in cursor.fetchall()]

    def cancel_job(self, job_id: str) -> Tuple[bool, str]:
        """
        Attempts to cancel an active job.
        Refuses if the job has already been published to Instagram.
        """
        job = self.get_job(job_id)
        if not job:
            return False, f"Job `{job_id}` not found."

        current_status = job.get("status")
        if current_status == "published":
            return False, "Reel is already live on Instagram and cannot be cancelled."

        if current_status == "cancelled":
            return False, "Job is already cancelled."

        now = _utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'cancelled',
                    error_message = 'Cancelled by user',
                    updated_at = ?
                WHERE job_id = ?
                """,
                (now, job_id),
            )
            conn.commit()

        self._cleanup_files_for_job(job_id)
        return True, "Job has been cancelled and local files removed."

    def get_job_resumption_stage(self, job_id: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Determines the appropriate pipeline stage to resume from:
        - 'publish': if video is hosted and has SAS URL
        - 'upload_to_azure': if processed video file exists
        - 'process': if downloaded source file exists
        - 'download': if no local artifacts exist
        """
        job = self.get_job(job_id)
        if not job:
            return False, "Job not found", {}

        if job["status"] == "published":
            return False, "Job is already published", job

        # Check for processed video on disk
        processed_file = PROCESSED_DIR / f"{job_id}.mp4"
        if processed_file.exists() and processed_file.stat().st_size > 0:
            if job.get("video_sas_url"):
                return True, "publish", job
            return True, "upload_to_azure", job

        # Check for raw download on disk
        download_file = DOWNLOADS_DIR / f"{job_id}.mp4"
        if download_file.exists() and download_file.stat().st_size > 0:
            return True, "process", job

        return True, "download", job

    def reset_job_status(self, job_id: str, new_status: str) -> bool:
        """Resets job status for retry, clearing previous error message."""
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (new_status, now, job_id),
            )
            conn.commit()
            return cursor.rowcount > 0


# Global singleton instance
job_store = JobStore()

