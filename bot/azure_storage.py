"""Azure Blob Storage management for public video and cover hosting."""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContainerClient,
    ContentSettings,
    generate_blob_sas,
)

from bot.config import (
    AZURE_BLOB_CONTAINER,
    AZURE_STORAGE_CONNECTION_STRING,
    DEFAULT_COVER_PATH,
)

logger = logging.getLogger("bot.azure_storage")

DEFAULT_VIDEO_SAS_EXPIRY_HOURS = 4
DEFAULT_COVER_SAS_EXPIRY_HOURS = 24
DEFAULT_RETENTION_HOURS = 24


class AzureStorageManager:
    """
    Manages Azure Blob Storage lifecycle for Instagram Reels video and cover hosting:
    - Dedicated container management
    - Read-only SAS URL generation
    - Chunked video upload with retry backoff and integrity verification
    - Cached default cover upload
    - Public reachability verification via HTTP HEAD
    - Automated lifecycle cleanup of video blobs
    """

    def __init__(
        self,
        connection_string: Optional[str] = None,
        container_name: Optional[str] = None,
    ):
        self.connection_string = connection_string or AZURE_STORAGE_CONNECTION_STRING
        self.container_name = container_name or AZURE_BLOB_CONTAINER
        self._blob_service_client: Optional[BlobServiceClient] = None
        self._container_client: Optional[ContainerClient] = None

        # In-memory cache for default cover upload:
        # { "path_str": { "mtime": float, "blob_name": str, "sas_url": str, "expires_at": datetime } }
        self._cover_cache: Dict[str, Dict[str, Any]] = {}

    @property
    def blob_service_client(self) -> BlobServiceClient:
        """Lazily initializes the BlobServiceClient."""
        if self._blob_service_client is None:
            self._blob_service_client = BlobServiceClient.from_connection_string(
                self.connection_string
            )
        return self._blob_service_client

    @blob_service_client.setter
    def blob_service_client(self, client: BlobServiceClient) -> None:
        self._blob_service_client = client

    @property
    def container_client(self) -> ContainerClient:
        """Lazily initializes the ContainerClient."""
        if self._container_client is None:
            self._container_client = self.blob_service_client.get_container_client(
                self.container_name
            )
        return self._container_client

    @container_client.setter
    def container_client(self, client: ContainerClient) -> None:
        self._container_client = client

    def ensure_container_exists(self) -> bool:
        """
        Creates the dedicated container if it does not already exist.
        Returns True if the container exists or was created, False on failure.
        """
        try:
            self.container_client.create_container()
            logger.info("Created dedicated Azure Blob container: '%s'", self.container_name)
            return True
        except ResourceExistsError:
            logger.debug("Dedicated Azure Blob container '%s' already exists.", self.container_name)
            return True
        except Exception as exc:
            logger.error("Failed to ensure container '%s' exists: %s", self.container_name, exc)
            raise

    def generate_sas_url(
        self,
        blob_name: str,
        expiry_hours: int = DEFAULT_VIDEO_SAS_EXPIRY_HOURS,
    ) -> Tuple[str, datetime]:
        """
        Generates a read-only HTTPS SAS URL for a specific blob.
        Returns (sas_url, expiry_datetime_utc).
        """
        client = self.blob_service_client
        account_name = client.account_name
        account_key = getattr(client.credential, "account_key", None)

        if not account_key:
            raise RuntimeError("Azure storage connection credential does not contain an account key.")

        expiry = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)

        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=self.container_name,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )

        blob_client = self.container_client.get_blob_client(blob_name)
        sas_url = f"{blob_client.url}?{sas_token}"
        return sas_url, expiry

    async def check_blob_reachability(
        self,
        sas_url: str,
        timeout_seconds: float = 10.0,
    ) -> Tuple[bool, str]:
        """
        Performs an asynchronous HTTP HEAD request against the SAS URL
        to verify that external systems (such as Instagram's ingestion servers)
        can download the file without authentication.
        """
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
                resp = await client.head(sas_url)
                if resp.status_code in (200, 206):
                    return True, f"HTTP {resp.status_code}"
                return False, f"HTTP {resp.status_code}: {resp.reason_phrase}"
        except httpx.TimeoutException:
            return False, "HTTP request timed out"
        except Exception as exc:
            return False, f"Reachability check error: {exc}"

    async def upload_video_blob(
        self,
        job_id: str,
        file_path: Path,
        max_retries: int = 3,
        initial_backoff: float = 1.0,
        expiry_hours: int = DEFAULT_VIDEO_SAS_EXPIRY_HOURS,
    ) -> Dict[str, Any]:
        """
        Uploads a processed video file as '{job_id}.mp4' to Azure Blob Storage:
        - Uploads with retries and exponential backoff
        - Sets video/mp4 MIME content settings
        - Validates uploaded blob size matches local file size
        - Generates 4-hour read-only SAS URL
        - Validates external public reachability via HTTP HEAD
        Returns metadata dict containing blob_name, sas_url, expires_at, and size.
        """
        path = Path(file_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Video file not found at {path}")

        local_size = path.stat().st_size
        if local_size == 0:
            raise ValueError(f"Video file is empty (0 bytes): {path}")

        blob_name = f"{job_id}.mp4"
        blob_client = self.container_client.get_blob_client(blob_name)

        last_exc: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    "[%s] Uploading video to Azure Blob '%s' (attempt %d/%d, %d bytes)...",
                    job_id,
                    blob_name,
                    attempt,
                    max_retries,
                    local_size,
                )
                with open(path, "rb") as f:
                    # Run synchronous upload in thread pool to avoid blocking asyncio event loop
                    await asyncio.to_thread(
                        blob_client.upload_blob,
                        f,
                        overwrite=True,
                        content_settings=ContentSettings(content_type="video/mp4"),
                    )
                logger.info("[%s] Video upload to Azure Blob '%s' finished.", job_id, blob_name)
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "[%s] Azure upload attempt %d/%d failed: %s",
                    job_id,
                    attempt,
                    max_retries,
                    exc,
                )
                if attempt < max_retries:
                    backoff = initial_backoff * (2 ** (attempt - 1))
                    await asyncio.sleep(backoff)
                else:
                    raise RuntimeError(
                        f"Failed to upload video to Azure after {max_retries} attempts: {last_exc}"
                    ) from last_exc

        # Verify blob existence and size
        try:
            properties = await asyncio.to_thread(blob_client.get_blob_properties)
            if properties.size != local_size:
                raise ValueError(
                    f"Uploaded blob size mismatch: expected {local_size} bytes, found {properties.size} bytes."
                )
        except Exception as exc:
            raise RuntimeError(f"Integrity check failed for uploaded blob '{blob_name}': {exc}") from exc

        # Generate SAS URL
        sas_url, expiry = self.generate_sas_url(blob_name, expiry_hours=expiry_hours)

        # External reachability check (HEAD request)
        reachable, reach_detail = await self.check_blob_reachability(sas_url)
        if not reachable:
            logger.error(
                "[%s] Uploaded video SAS URL is not publicly reachable: %s",
                job_id,
                reach_detail,
            )
            raise RuntimeError(
                f"Uploaded video blob failed public reachability check: {reach_detail}"
            )

        logger.info(
            "[%s] Video successfully uploaded and verified: blob='%s', size=%d, reachability=%s",
            job_id,
            blob_name,
            local_size,
            reach_detail,
        )

        return {
            "blob_name": blob_name,
            "sas_url": sas_url,
            "expires_at": expiry.isoformat(),
            "size": local_size,
        }

    async def ensure_cover_uploaded(
        self,
        cover_path: Optional[Path] = None,
        expiry_hours: int = DEFAULT_COVER_SAS_EXPIRY_HOURS,
    ) -> Dict[str, Any]:
        """
        Uploads the default cover image once to Azure Blob Storage and caches the result.
        Re-uploads only if the local file's modification time changes or the SAS URL expires soon.
        """
        path = Path(cover_path or DEFAULT_COVER_PATH).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Cover image not found at {path}")

        path_key = str(path)
        current_mtime = path.stat().st_mtime
        local_size = path.stat().st_size
        now = datetime.now(timezone.utc)

        # Check cache
        cached = self._cover_cache.get(path_key)
        if cached:
            cached_mtime = cached.get("mtime")
            expires_at: Optional[datetime] = cached.get("expires_dt")
            # If mtime matches and SAS is valid for at least 2 more hours, return cached
            if (
                cached_mtime == current_mtime
                and expires_at
                and expires_at > (now + timedelta(hours=2))
            ):
                logger.debug("Using cached Azure cover SAS URL for '%s'", path.name)
                return cached["data"]

        # Determine extension and MIME type
        ext = path.suffix.lstrip(".").lower()
        if ext not in ("jpg", "jpeg", "png"):
            ext = "jpg"
        content_type = "image/png" if ext == "png" else "image/jpeg"
        blob_name = f"cover.{ext}"

        blob_client = self.container_client.get_blob_client(blob_name)

        logger.info("Uploading fixed cover image '%s' to Azure Blob as '%s'...", path.name, blob_name)
        with open(path, "rb") as f:
            await asyncio.to_thread(
                blob_client.upload_blob,
                f,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )

        # Verify size
        props = await asyncio.to_thread(blob_client.get_blob_properties)
        if props.size != local_size:
            raise ValueError(
                f"Uploaded cover size mismatch: expected {local_size} bytes, got {props.size} bytes."
            )

        # Generate SAS URL
        sas_url, expiry = self.generate_sas_url(blob_name, expiry_hours=expiry_hours)

        # Reachability check
        reachable, reach_detail = await self.check_blob_reachability(sas_url)
        if not reachable:
            raise RuntimeError(f"Uploaded cover blob failed public reachability check: {reach_detail}")

        data = {
            "blob_name": blob_name,
            "sas_url": sas_url,
            "expires_at": expiry.isoformat(),
            "size": local_size,
        }

        # Cache result
        self._cover_cache[path_key] = {
            "mtime": current_mtime,
            "blob_name": blob_name,
            "sas_url": sas_url,
            "expires_dt": expiry,
            "data": data,
        }

        logger.info(
            "Default cover successfully uploaded: blob='%s', size=%d, reachability=%s",
            blob_name,
            local_size,
            reach_detail,
        )
        return data

    def delete_job_video_blob(self, job_id: str) -> bool:
        """
        Deletes the video blob '{job_id}.mp4' from Azure Blob Storage.
        Returns True if deleted, False if not found.
        """
        blob_name = f"{job_id}.mp4"
        blob_client = self.container_client.get_blob_client(blob_name)
        try:
            blob_client.delete_blob(delete_snapshots="include")
            logger.info("[%s] Deleted video blob '%s' from Azure Blob Storage.", job_id, blob_name)
            return True
        except ResourceNotFoundError:
            logger.warning("[%s] Video blob '%s' not found for deletion.", job_id, blob_name)
            return False
        except Exception as exc:
            logger.error("[%s] Error deleting video blob '%s': %s", job_id, blob_name, exc)
            raise

    def cleanup_expired_video_blobs(
        self,
        max_age_hours: int = DEFAULT_RETENTION_HOURS,
    ) -> List[str]:
        """
        Safety net retention sweep: deletes video blobs older than max_age_hours.
        NEVER deletes the cover image blob (cover.*).
        Returns a list of deleted blob names.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        deleted_blobs: List[str] = []

        try:
            blobs = self.container_client.list_blobs()
            for blob in blobs:
                name = blob.name
                # Only target video blobs (.mp4) and NEVER cover images
                if not name.lower().endswith(".mp4"):
                    continue
                if name.lower().startswith("cover."):
                    continue

                last_modified = blob.last_modified
                if last_modified and last_modified < cutoff:
                    try:
                        self.container_client.delete_blob(name, delete_snapshots="include")
                        deleted_blobs.append(name)
                        logger.info(
                            "Lifecycle cleanup: deleted expired video blob '%s' (last modified: %s)",
                            name,
                            last_modified.isoformat(),
                        )
                    except Exception as err:
                        logger.warning("Failed to delete expired blob '%s': %s", name, err)
        except Exception as exc:
            logger.error("Failed during lifecycle cleanup sweep: %s", exc)

        return deleted_blobs


# Global singleton instance
azure_storage_manager = AzureStorageManager()
