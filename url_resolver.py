"""
Resolve short URLs (shp.ee, s.shopee.vn) → full Shopee product URL.
"""

import httpx
import logging

logger = logging.getLogger(__name__)

SHORT_DOMAINS = ("shp.ee", "s.shopee.vn")


def is_short_url(url: str) -> bool:
    for d in SHORT_DOMAINS:
        if d in url:
            return True
    return False


async def resolve_redirect(url: str) -> str:
    """Follow 301/302 redirect to get the final URL."""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=8
        ) as client:
            resp = await client.head(url)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", url)
                # Đôi khi redirect nhiều bước
                if is_short_url(location):
                    return await resolve_redirect(location)
                return location
    except Exception as e:
        logger.warning("resolve_redirect failed for %s: %s", url, e)
    return url
