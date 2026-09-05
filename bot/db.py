"""SQLite job store for tracking download and processing pipelines."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
DB_PATH = STORAGE_DIR / "jobs.db"
DOWNLOADS_DIR = STORAGE_DIR / "downloads"


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
        """Creates the jobs table and indexes if not already present."""
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id)"
            )
            conn.commit()

    def create_job(self, job_id: str, user_id: int, source_url: str) -> Dict[str, Any]:
        """Registers a new job in 'pending' status."""
        now = _utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, user_id, source_url, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (job_id, user_id, source_url, now, now),
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

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single job by its UUID."""
        with self._connection() as conn:
            cursor = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

    def list_jobs_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Returns all jobs matching a given status."""
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
            return [dict(r) for r in cursor.fetchall()]

    def recover_interrupted_jobs(self) -> List[str]:
        """
        Recovers jobs left in 'pending' or 'downloading' states on bot restart.
        Marks them as 'failed' and cleans up any partial downloads.
        Returns list of recovered job IDs.
        """
        recovered_ids = []
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT job_id, file_path FROM jobs WHERE status IN ('pending', 'downloading')"
            )
            rows = cursor.fetchall()
            for row in rows:
                jid = row["job_id"]
                recovered_ids.append(jid)
                # Cleanup any potential leftover partial or completed file
                self._cleanup_files_for_job(jid)

            if recovered_ids:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed',
                        error_message = 'Interrupted by bot restart',
                        updated_at = ?
                    WHERE status IN ('pending', 'downloading')
                    """,
                    (now,),
                )
                conn.commit()
        return recovered_ids

    def _cleanup_files_for_job(self, job_id: str) -> None:
        """Removes any files matching the job_id pattern from storage/downloads."""
        if not DOWNLOADS_DIR.exists():
            return
        for file in DOWNLOADS_DIR.glob(f"{job_id}.*"):
            try:
                if file.is_file():
                    file.unlink(missing_ok=True)
            except Exception:
                pass


# Global singleton instance
job_store = JobStore()
