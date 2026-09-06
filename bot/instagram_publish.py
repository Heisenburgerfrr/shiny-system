"""Instagram Graph API Content Publishing engine for Instagram Reels."""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

import httpx

from bot.azure_storage import azure_storage_manager
from bot.config import (
    INSTAGRAM_ACCESS_TOKEN,
    INSTAGRAM_BUSINESS_ACCOUNT_ID,
    INSTAGRAM_CAPTION_SUFFIX,
    INSTAGRAM_PUBLISH_RATE_LIMIT,
)
from bot.db import job_store

logger = logging.getLogger("bot.instagram_publish")

GRAPH_API_VERSION = "v21.0"
BASE_GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_MAX_POLL_TIMEOUT_SECONDS = 600.0  # 10 minutes


class InstagramPublishError(Exception):
    """Base exception for all Instagram Graph API publishing errors."""
    pass


class InstagramTokenExpiredError(InstagramPublishError):
    """Raised when the Instagram access token is invalid or expired (OAuthException / code 190)."""
    pass


class InstagramRateLimitError(InstagramPublishError):
    """Raised when Meta rate limits or the 24-hour publish ceiling is reached."""
    pass


class InstagramMediaSpecError(InstagramPublishError):
    """Raised when the media does not meet Instagram Reels technical specs."""
    pass


class InstagramTimeoutError(InstagramPublishError):
    """Raised when container status polling exceeds the maximum allotted duration."""
    pass


def _classify_meta_error(error_data: Dict[str, Any], status_code: int) -> InstagramPublishError:
    """Parses Meta Graph API error payload and returns a domain-specific exception."""
    code = error_data.get("code")
    subcode = error_data.get("error_subcode")
    message = error_data.get("message", "Unknown Graph API error")
    error_type = error_data.get("type", "")

    # Token expiration or invalid authentication (Meta uses error code 190 or explicit token subcodes)
    if code == 190 or subcode in (458, 459, 460, 463, 467, 490) or "session has expired" in message.lower() or "error validating access token" in message.lower():
        return InstagramTokenExpiredError(
            f"Instagram access token is expired or invalid (code {code}, subcode {subcode}): {message}"
        )

    # Rate limiting
    if code in (4, 17, 32) or "rate limit" in message.lower():
        return InstagramRateLimitError(
            f"Instagram Content Publishing rate limit exceeded (code {code}): {message}"
        )

    # Media specification rejection
    if code in (2207050, 2207051, 2207052) or "aspect ratio" in message.lower() or "codec" in message.lower():
        return InstagramMediaSpecError(
            f"Media rejected by Instagram Reels specification (code {code}): {message}"
        )

    return InstagramPublishError(f"HTTP {status_code} [{error_type} {code}]: {message}")


class InstagramPublisher:
    """
    Executes the 3-step Instagram Reels publishing workflow:
    1. Create media container (media_type=REELS, video_url, cover_url, caption)
    2. Poll container status until FINISHED (with progress reporting & 10m timeout)
    3. Publish media container
    4. Fetch live permalink and trigger Azure temporary blob cleanup
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        business_account_id: Optional[str] = None,
        base_url: str = BASE_GRAPH_URL,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        max_poll_timeout: float = DEFAULT_MAX_POLL_TIMEOUT_SECONDS,
    ):
        self.access_token = access_token or INSTAGRAM_ACCESS_TOKEN
        self.business_account_id = business_account_id or INSTAGRAM_BUSINESS_ACCOUNT_ID
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.max_poll_timeout = max_poll_timeout

    def check_rate_limit(self) -> Tuple[bool, int]:
        """
        Validates whether publishing another Reel would exceed the 24-hour rate limit.
        Returns (is_allowed: bool, current_24h_count: int).
        """
        count = job_store.count_published_in_last_24h()
        limit = INSTAGRAM_PUBLISH_RATE_LIMIT
        if count >= limit:
            logger.warning(
                "24-hour publish rate limit reached: %d / %d posts in rolling window.",
                count,
                limit,
            )
            return False, count
        return True, count

    async def create_reels_container(
        self,
        video_url: str,
        caption: str,
        cover_url: Optional[str] = None,
    ) -> str:
        """
        Step 1: Creates an Instagram Reels media container.
        POST /{ig-user-id}/media
        Returns the container ID.
        """
        endpoint = f"{self.base_url}/{self.business_account_id}/media"
        payload = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": self.access_token,
        }
        if cover_url:
            payload["cover_url"] = cover_url

        logger.info(
            "Creating Instagram Reels container for account %s...",
            self.business_account_id,
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(endpoint, data=payload)
            data = resp.json()

        if resp.status_code != 200 or "id" not in data:
            err_dict = data.get("error", {})
            exc = _classify_meta_error(err_dict, resp.status_code)
            logger.error("Failed to create Reels container: %s", exc)
            raise exc

        container_id = data["id"]
        logger.info("Instagram Reels container created: container_id=%s", container_id)
        return container_id

    async def poll_container_status(
        self,
        container_id: str,
        progress_callback: Optional[Callable[[str, float], Awaitable[None]]] = None,
    ) -> str:
        """
        Step 2: Polls container status until 'FINISHED'.
        GET /{container-id}?fields=status_code,status
        Caps total wait time at max_poll_timeout (10 minutes).
        """
        endpoint = f"{self.base_url}/{container_id}"
        params = {
            "fields": "status_code,status",
            "access_token": self.access_token,
        }

        start_time = time.monotonic()
        logger.info("Polling container %s until FINISHED (timeout=%0.1fs)...", container_id, self.max_poll_timeout)

        async with httpx.AsyncClient(timeout=15.0) as client:
            while True:
                resp = await client.get(endpoint, params=params)
                data = resp.json()
                elapsed = time.monotonic() - start_time

                if resp.status_code != 200:
                    err_dict = data.get("error", {})
                    exc = _classify_meta_error(err_dict, resp.status_code)
                    logger.error("Error checking container status: %s", exc)
                    raise exc

                status_code = data.get("status_code", "").upper()
                status_detail = data.get("status", "")

                logger.debug(
                    "Container %s status: %s (%s) [elapsed: %.1fs]",
                    container_id,
                    status_code,
                    status_detail,
                    elapsed,
                )

                if status_code == "FINISHED":
                    logger.info("Container %s successfully processed in %.1fs.", container_id, elapsed)
                    return container_id

                if status_code == "ERROR":
                    err_msg = status_detail or "Meta reported an unrecoverable container processing error."
                    raise InstagramPublishError(f"Container processing error: {err_msg}")

                if status_code == "EXPIRED":
                    raise InstagramPublishError("Container expired before it could be published.")

                # Still IN_PROGRESS
                if progress_callback:
                    try:
                        await progress_callback(status_code, elapsed)
                    except Exception as cb_err:
                        logger.debug("Progress callback exception (ignored): %s", cb_err)

                if elapsed >= self.max_poll_timeout:
                    raise InstagramTimeoutError(
                        f"Instagram video processing timed out after {int(elapsed)} seconds. "
                        "The container was not ready in time."
                    )

                await asyncio.sleep(self.poll_interval)

    async def publish_container(self, container_id: str, max_retries: int = 5) -> str:
        """
        Step 3: Publishes the finished container with automatic retries for transient Meta server errors (subcode 2207085, code -1, 5xx).
        POST /{ig-user-id}/media_publish
        Returns the published media ID.
        """
        endpoint = f"{self.base_url}/{self.business_account_id}/media_publish"
        payload = {
            "creation_id": container_id,
            "access_token": self.access_token,
        }

        for attempt in range(1, max_retries + 1):
            logger.info("Publishing container %s (attempt %d/%d)...", container_id, attempt, max_retries)

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(endpoint, data=payload)
                data = resp.json()

            if resp.status_code == 200 and "id" in data:
                media_id = data["id"]
                logger.info("Container %s published live: media_id=%s", container_id, media_id)
                return media_id

            err_dict = data.get("error", {})
            exc = _classify_meta_error(err_dict, resp.status_code)
            code = err_dict.get("code", 0)
            subcode = err_dict.get("error_subcode", 0)

            # Meta subcode 2207085 / code -1 is documented as a transient internal server error
            is_transient = (
                code in (-1, 1, 2)
                or subcode == 2207085
                or resp.status_code >= 500
            )

            # Check if Meta actually published the Reel despite returning an error code
            if is_transient:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as verify_client:
                        v_resp = await verify_client.get(
                            f"{self.base_url}/{self.business_account_id}/media",
                            params={"fields": "id,timestamp", "limit": "2", "access_token": self.access_token},
                        )
                        if v_resp.status_code == 200:
                            recent_items = v_resp.json().get("data", [])
                            if recent_items:
                                latest_media_id = recent_items[0].get("id")
                                logger.info("Detected published media on account: media_id=%s", latest_media_id)
                                return latest_media_id
                except Exception as check_exc:
                    logger.debug("Non-fatal error verifying recent media: %s", check_exc)

            if is_transient and attempt < max_retries:
                backoff = attempt * 3.0  # 3s, 6s, 9s, 12s
                logger.warning(
                    "Publish attempt %d for container %s encountered transient Meta error (code %s, subcode %s). Retrying in %.1fs...",
                    attempt,
                    container_id,
                    code,
                    subcode,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue

            logger.error("Failed to publish container %s: %s", container_id, exc)
            raise exc

    async def get_media_permalink(self, media_id: str) -> str:
        """
        Fetches the public permalink for the published media ID.
        GET /{media-id}?fields=permalink,media_type
        """
        endpoint = f"{self.base_url}/{media_id}"
        params = {
            "fields": "permalink,media_type",
            "access_token": self.access_token,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(endpoint, params=params)
            data = resp.json()

        if resp.status_code == 200 and "permalink" in data:
            permalink = data["permalink"]
            logger.info("Retrieved permalink for media %s: %s", media_id, permalink)
            return permalink

        # Fallback to standard Instagram reel URL if permalink field is delayed
        fallback = f"https://www.instagram.com/reel/{media_id}/"
        logger.warning("Permalink not found in response, falling back to %s", fallback)
        return fallback

    async def publish_reel(
        self,
        job_id: str,
        video_url: str,
        caption: str,
        cover_url: Optional[str] = None,
        progress_callback: Optional[Callable[[str, float], Awaitable[None]]] = None,
    ) -> Dict[str, str]:
        """
        Full end-to-end Reels publishing pipeline:
        1. Rate limit check
        2. Create container
        3. Poll until FINISHED
        4. Publish container
        5. Fetch live permalink
        6. Clean up temporary Azure video blob
        """
        # 1. Rate limit safety check
        allowed, count = self.check_rate_limit()
        if not allowed:
            raise InstagramRateLimitError(
                f"Instagram 24-hour publishing limit reached ({count} posts). "
                "Please wait before posting more Reels."
            )

        # 2. Append optional configured caption template / suffix
        final_caption = caption.strip()
        if INSTAGRAM_CAPTION_SUFFIX and INSTAGRAM_CAPTION_SUFFIX not in final_caption:
            final_caption = f"{final_caption}\n{INSTAGRAM_CAPTION_SUFFIX}".strip()

        # 3. Create container
        container_id = await self.create_reels_container(
            video_url=video_url,
            caption=final_caption,
            cover_url=cover_url,
        )
        job_store.record_container_created(job_id, container_id)

        # 4. Poll status
        await self.poll_container_status(container_id, progress_callback=progress_callback)

        # 5. Publish container
        media_id = await self.publish_container(container_id)

        # 6. Fetch live permalink
        permalink = await self.get_media_permalink(media_id)

        # 7. Update SQLite record
        job_store.complete_publishing(job_id=job_id, media_id=media_id, permalink=permalink)

        # 8. Post-publish cleanup: remove temporary Azure video blob
        try:
            azure_storage_manager.delete_job_video_blob(job_id)
            logger.info("[%s] Successfully cleaned up temporary Azure video blob.", job_id)
        except Exception as cleanup_err:
            logger.warning("[%s] Non-fatal error cleaning up Azure video blob: %s", job_id, cleanup_err)

        return {
            "container_id": container_id,
            "media_id": media_id,
            "permalink": permalink,
            "caption": final_caption,
        }


# Global singleton instance
instagram_publisher = InstagramPublisher()
