"""
Sinh link affiliate Shopee offline (format an_redir).
Port từ shopee-aff/backend/app/services/linkgen.py — không cần Open API.
"""

import re
from urllib.parse import quote, urlencode, urlparse, parse_qs

BASE = "https://s.shopee.vn/an_redir"

# Shopee sub_id chỉ chấp nhận A-Z 0-9, dấu '-' là ký tự phân tách
_SUBID_CLEAN = re.compile(r"[^A-Z0-9]")

# Regex bắt URL Shopee (kể cả rút gọn shp.ee)
SHOPEE_URL_RE = re.compile(
    r"(?:https?://)?(?:[a-zA-Z0-9_-]+\.)*(?:shopee\.vn|shp\.ee)(?:/[^\s<>\"'{}|\\^`\[\]]*)?",
    re.IGNORECASE,
)


def clean_subid(value: str) -> str:
    return _SUBID_CLEAN.sub("", (value or "").upper())


ITEM_RE = re.compile(r"i\.(\d+)\.(\d+)")


def _shorten_shopee_url(url: str) -> str:
    m = ITEM_RE.search(url)
    if m:
        return f"https://shopee.vn/product/{m.group(1)}/{m.group(2)}"
    return url


def build_affiliate_link(
    product_url: str,
    affiliate_id: str,
    sub_ids: list[str] | None = None,
) -> str:
    if not product_url.startswith("http"):
        product_url = "https://" + product_url

    product_url = _shorten_shopee_url(product_url)

    subs = [clean_subid(s) for s in (sub_ids or [])][:5]
    while len(subs) < 5:
        subs.append("")
    sub_id = "-".join(subs).rstrip("-")

    params = {"origin_link": product_url, "affiliate_id": str(affiliate_id)}
    if sub_id:
        params["sub_id"] = sub_id

    return f"{BASE}?{urlencode(params, quote_via=quote)}"


def extract_shopee_urls(text: str) -> list[str]:
    """Trích xuất & dedupe tất cả URL Shopee từ đoạn text."""
    matches = SHOPEE_URL_RE.findall(text or "")
    seen: set[str] = set()
    urls: list[str] = []
    for m in matches:
        cleaned = m.strip().rstrip(".,;:!?)]}>\"'")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)
    return urls
