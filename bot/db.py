"""SQLite job store for tracking download, processing, and publishing pipelines."""

import json
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
            # Check for processing_instructions column in existing DBs
            cursor = conn.execute("PRAGMA table_info(jobs)")
            columns = [row["name"] for row in cursor.fetchall()]
            if "processing_instructions" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN processing_instructions TEXT")

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

    def recover_interrupted_jobs(self) -> List[str]:
        """
        Recovers jobs left in 'pending', 'downloading', or 'processing' states on bot restart.
        Marks them as 'failed' or 'processing_failed' and cleans up partial files.
        Returns list of recovered job IDs.
        """
        recovered_ids = []
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT job_id, status FROM jobs WHERE status IN ('pending', 'downloading', 'processing')"
            )
            rows = cursor.fetchall()
            for row in rows:
                jid = row["job_id"]
                st = row["status"]
                recovered_ids.append(jid)
                self._cleanup_files_for_job(jid)

            if recovered_ids:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = CASE
                            WHEN status = 'processing' THEN 'processing_failed'
                            ELSE 'failed'
                        END,
                        error_message = 'Interrupted by bot restart',
                        updated_at = ?
                    WHERE status IN ('pending', 'downloading', 'processing')
                    """,
                    (now,),
                )
                conn.commit()
        return recovered_ids

    def _cleanup_files_for_job(self, job_id: str) -> None:
        """Removes partial files matching the job_id pattern from storage."""
        for d in [DOWNLOADS_DIR, PROCESSED_DIR]:
            if not d.exists():
                continue
            for file in d.glob(f"{job_id}.*"):
                try:
                    if file.is_file():
                        file.unlink(missing_ok=True)
                except Exception:
                    pass


# Global singleton instance
job_store = JobStore()
