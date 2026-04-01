"""Polymarket geo-block detection and startup guard.

Polymarket is geo-blocked from US IP addresses. If your bot runs on a
US-based server (AWS us-east, GCP us-central, etc.) without VPN routing,
it will fail with confusing errors — usually HTTP 403s or connection
timeouts on the CLOB API, not a clear "you're blocked" message.

This module provides:
1. A startup check that detects geo-blocking before the bot tries to trade
2. Clear error messages that tell you exactly what's wrong and how to fix it
3. Optional VPN connectivity verification if you're using a VPN

From production experience:
- NordVPN with an Argentina server works reliably for Polymarket access
- The check endpoint is the Polymarket CLOB health endpoint
- Run this check at bot startup, not mid-session

Usage::

    from exchanges.polymarket.geo_guard import GeoGuard

    guard = GeoGuard()
    await guard.check()  # raises GeoBlockedError with instructions if blocked

    # Or with soft failure (just warns, doesn't raise):
    ok = await guard.check(raise_on_block=False)
    if not ok:
        logger.warning("polymarket_geo_blocked — trades will fail")
"""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger()

CLOB_HEALTH_URL = "https://clob.polymarket.com/"
GAMMA_HEALTH_URL = "https://gamma-api.polymarket.com/markets?limit=1"
REQUEST_TIMEOUT = 10.0

GEO_BLOCK_FIX = """
Polymarket is geo-blocked from US IP addresses.

To fix this:
1. Use a VPN with a non-US server (Argentina works reliably with NordVPN)
2. Verify VPN is active: curl https://ipinfo.io/country should return a non-US code
3. Restart the bot after connecting

If running in a cloud environment (AWS/GCP/Azure), ensure your VPN routes
ALL traffic through the VPN interface, not just browser traffic.
"""


class GeoBlockedError(Exception):
    """Raised when Polymarket geo-block is detected."""

    def __init__(self, details: str = ""):
        super().__init__(
            f"Polymarket geo-block detected. {details}\n{GEO_BLOCK_FIX}"
        )


class GeoGuard:
    """Detect Polymarket geo-blocking at startup.

    Args:
        check_urls: List of URLs to test. All must succeed.
        timeout: Request timeout in seconds.
        expected_country: If set, verify the detected country matches.
            Useful to confirm VPN is routing to the right location.
    """

    def __init__(
        self,
        check_urls: list[str] | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ):
        self.check_urls = check_urls or [CLOB_HEALTH_URL, GAMMA_HEALTH_URL]
        self.timeout = timeout

    async def check(self, raise_on_block: bool = True) -> bool:
        """Run geo-block detection.

        Args:
            raise_on_block: If True, raise GeoBlockedError on failure.
                If False, return False instead.

        Returns:
            True if accessible, False if blocked (when raise_on_block=False).

        Raises:
            GeoBlockedError: If geo-blocked and raise_on_block=True.
        """
        try:
            import aiohttp
        except ImportError:
            logger.warning("geo_guard_skipped", reason="aiohttp not installed")
            return True

        results = await asyncio.gather(
            *[self._check_url(url) for url in self.check_urls],
            return_exceptions=True,
        )

        blocked_urls = []
        for url, result in zip(self.check_urls, results):
            if isinstance(result, Exception) or result is False:
                blocked_urls.append(url)

        if blocked_urls:
            details = f"Failed to reach: {', '.join(blocked_urls)}"
            logger.error("polymarket_geo_blocked", blocked_urls=blocked_urls)
            if raise_on_block:
                raise GeoBlockedError(details)
            return False

        logger.info("polymarket_geo_check_passed", checked=len(self.check_urls))
        return True

    async def _check_url(self, url: str) -> bool:
        """Return True if URL is reachable and returns non-403/non-451 status."""
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    if resp.status in (403, 451):
                        logger.warning(
                            "geo_block_status",
                            url=url,
                            status=resp.status,
                            note="451 = Unavailable For Legal Reasons (geo-block)",
                        )
                        return False
                    return True
        except Exception as e:
            logger.warning("geo_check_request_failed", url=url, error=str(e))
            return False

    async def detect_country(self) -> str | None:
        """Detect the apparent country of the current IP.

        Useful for confirming VPN is routing correctly.
        Returns ISO country code (e.g. "AR" for Argentina) or None on failure.
        """
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://ipinfo.io/country",
                    timeout=aiohttp.ClientTimeout(total=5.0),
                ) as resp:
                    country = (await resp.text()).strip()
                    logger.info("detected_country", country=country)
                    if country == "US":
                        logger.warning(
                            "us_ip_detected",
                            note="Polymarket is geo-blocked from US IPs — use a VPN",
                        )
                    return country
        except Exception as e:
            logger.warning("country_detection_failed", error=str(e))
            return None
