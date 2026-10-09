"""
Shopee Affiliate scraper — dùng cookies + httpx gọi trực tiếp internal API.
- Nhận cookies từ JSON upload (export từ browser extension)
- Gọi /api/v3/report/list lấy conversion data
- Match sub_id → zalo_id → cập nhật ví user
- State + cookies lưu trong Turso DB (kv_store)
"""

import json
import logging
import time
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

AFF_API = "https://affiliate.shopee.vn/api"

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9",
    "Referer": "https://affiliate.shopee.vn/offer/report",
    "Origin": "https://affiliate.shopee.vn",
}


async def _load_state() -> dict:
    from db import kv_get
    raw = await kv_get("shopee_state")
    return json.loads(raw) if raw else {}


async def _save_state(data: dict):
    from db import kv_set
    state = await _load_state()
    state.update(data)
    await kv_set("shopee_state", json.dumps(state, ensure_ascii=False))


async def _load_cookies() -> list:
    from db import kv_get
    raw = await kv_get("shopee_cookies")
    return json.loads(raw) if raw else []


async def _save_cookies(cookies: list):
    from db import kv_set
    await kv_set("shopee_cookies", json.dumps(cookies, ensure_ascii=False))


async def _cookie_string() -> str:
    cookies = await _load_cookies()
    if not cookies:
        return ""
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


async def get_scraper_status() -> dict:
    cookies = await _load_cookies()
    has_cookies = len(cookies) > 0
    state = await _load_state()
    return {
        "has_cookies": has_cookies,
        "last_sync": state.get("last_sync"),
        "last_sync_count": state.get("last_sync_count", 0),
        "total_synced": state.get("total_synced", 0),
        "cookie_user": state.get("cookie_user", ""),
        "sync_interval_hours": state.get("sync_interval_hours", 6),
        "status": "ready" if has_cookies else "need_cookie",
    }


# ──────────────────────────────────────────────
# COOKIES: nhận từ JSON upload
# ──────────────────────────────────────────────

async def save_cookies_from_json(cookies_data: list) -> dict:
    if not isinstance(cookies_data, list) or len(cookies_data) == 0:
        return {"ok": False, "error": "JSON không hợp lệ — cần array of cookies"}

    pw_cookies = []
    for c in cookies_data:
        name = c.get("name", "")
        value = c.get("value", "")
        if not name or not value:
            continue
        pw_cookies.append({
            "name": name,
            "value": value,
            "domain": c.get("domain", ".shopee.vn"),
            "path": c.get("path", "/"),
        })

    if not pw_cookies:
        return {"ok": False, "error": "Không tìm thấy cookies hợp lệ"}

    has_session = any(c["name"] in ("SPC_F", "SPC_SI", "SPC_ST") for c in pw_cookies)
    if not has_session:
        return {"ok": False, "error": "Cookies thiếu session (SPC_F/SPC_SI). Export lại từ affiliate.shopee.vn"}

    await _save_cookies(pw_cookies)
    await _save_state({"cookie_user": "", "login_at": datetime.now().isoformat()})
    return {"ok": True, "message": f"Đã lưu {len(pw_cookies)} cookies!", "cookie_count": len(pw_cookies)}


async def save_cookies_from_string(cookie_string: str) -> dict:
    cookies = []
    for item in cookie_string.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            cookies.append({
                "name": k.strip(),
                "value": v.strip(),
                "domain": ".shopee.vn",
                "path": "/",
            })
    if not cookies:
        return {"ok": False, "error": "Cookie string rỗng"}
    return await save_cookies_from_json(cookies)


# ──────────────────────────────────────────────
# VERIFY: kiểm tra cookies còn sống không
# ──────────────────────────────────────────────

async def verify_cookies() -> dict:
    cs = await _cookie_string()
    if not cs:
        return {"ok": False, "error": "Chưa có cookies"}

    headers = {**HEADERS_BASE, "Cookie": cs}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(f"{AFF_API}/v3/user/profile", headers=headers)
            if r.status_code == 200:
                data = r.json()
                if data.get("code") == 0:
                    profile = data.get("data", {})
                    user_name = profile.get("name") or profile.get("shopee_user_name", "")
                    aff_id = profile.get("affiliate_id", "")
                    await _save_state({"cookie_user": user_name, "affiliate_id": aff_id})
                    return {"ok": True, "user": user_name, "affiliate_id": aff_id}
            return {"ok": False, "error": "Cookie hết hạn hoặc không hợp lệ"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ──────────────────────────────────────────────
# SCRAPE: gọi API lấy conversion report
# ──────────────────────────────────────────────

async def scrape_conversions(days: int = 30) -> dict:
    cs = await _cookie_string()
    if not cs:
        return {"ok": False, "error": "Chưa có cookies. Upload cookies trước."}

    headers = {**HEADERS_BASE, "Cookie": cs}
    now = int(time.time())
    start = now - days * 86400

    try:
        all_items = []
        page_num = 1
        page_size = 50

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            check = await client.get(f"{AFF_API}/v3/user/status", headers=headers)
            if check.status_code != 200:
                return {"ok": False, "error": "Không thể kết nối Shopee API"}
            check_data = check.json()
            if check_data.get("code") != 0:
                return {"ok": False, "error": "Cookie hết hạn. Upload cookies mới."}

            while True:
                r = await client.get(
                    f"{AFF_API}/v3/report/list",
                    params={
                        "page_num": page_num,
                        "page_size": page_size,
                        "start_time": start,
                        "end_time": now,
                    },
                    headers=headers,
                )
                if r.status_code != 200:
                    break

                data = r.json()
                if data.get("code") != 0:
                    break

                items = data.get("data", {}).get("list") or []
                total = data.get("data", {}).get("total_count", 0)
                all_items.extend(items)

                if len(all_items) >= total or not items:
                    break
                page_num += 1

        conversions = _parse_items(all_items)
        state = await _load_state()

        await _save_state({
            "last_sync": datetime.now().isoformat(),
            "last_sync_count": len(conversions),
            "total_synced": state.get("total_synced", 0) + len(conversions),
        })

        return {
            "ok": True,
            "conversions": conversions,
            "count": len(conversions),
            "raw_count": len(all_items),
            "source": "api",
        }

    except Exception as e:
        logger.error("Scrape error: %s", e)
        return {"ok": False, "error": str(e)}


def _parse_items(items: list) -> list[dict]:
    conversions = []
    for item in items:
        conv = {
            "conversion_id": str(item.get("conversion_id") or item.get("id", "")),
            "order_id": str(item.get("order_id") or item.get("orderId", "")),
            "status": item.get("status") or item.get("order_status", ""),
            "commission": _num(item.get("total_commission") or item.get("commission", 0)),
            "seller_commission": _num(item.get("seller_commission", 0)),
            "shopee_commission": _num(item.get("shopee_commission", 0)),
            "purchase_time": item.get("purchase_time") or item.get("create_time", ""),
            "complete_time": item.get("complete_time", ""),
            "sub_id": item.get("utm_content") or item.get("sub_id", ""),
            "product_name": item.get("product_name") or item.get("item_name", ""),
            "amount": _num(item.get("actual_amount") or item.get("item_price", 0)),
            "shop_name": item.get("shop_name", ""),
        }
        if not conv["sub_id"] and isinstance(item.get("orders"), list):
            for order in item["orders"]:
                conv["order_id"] = conv["order_id"] or str(order.get("order_id", ""))
                conv["status"] = conv["status"] or order.get("order_status", "")
                if isinstance(order.get("items"), list):
                    for oi in order["items"]:
                        conv["product_name"] = conv["product_name"] or oi.get("item_name", "")
                        conv["amount"] = conv["amount"] or _num(oi.get("item_price", 0))
        conversions.append(conv)
    return conversions


def _num(val) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).replace(",", "").replace("đ", "").replace("₫", "").strip())
    except (ValueError, TypeError):
        return 0.0


# ──────────────────────────────────────────────
# SYNC: cập nhật ví user từ conversion data
# ──────────────────────────────────────────────

async def sync_commissions_to_wallets(conversions: list[dict]) -> dict:
    from db import credit_commission, log_notification
    from utils import fmt_vnd
    import config
    from zalo_api import send_text

    updated = 0
    notified = []

    for conv in conversions:
        sub_id = conv.get("sub_id", "").strip()
        commission = conv.get("commission", 0)
        status = str(conv.get("status", "")).upper()
        order_id = conv.get("order_id", "")

        if not sub_id or commission <= 0:
            continue
        if status in ("CANCELLED", "FRAUD", "CANCELED"):
            continue

        user_commission = round(commission * 0.3, 0)
        if user_commission <= 0:
            continue

        conv_id = conv.get("conversion_id", order_id)
        result = await credit_commission(sub_id, conv_id, order_id, user_commission)
        if not result.get("ok"):
            continue

        updated += 1
        notified.append({"zalo_id": sub_id, "commission": user_commission, "order_id": order_id})

    for n in notified:
        try:
            msg = (
                f"🎉 Tin vui! Bạn nhận được hoa hồng!\n"
                f"━━━━━━━━━━━━━━━\n"
                f"💰 +{fmt_vnd(n['commission'])} vào ví\n"
                f"📦 Đơn hàng: #{n['order_id']}\n\n"
                f"Gõ \"vi\" để xem số dư 👛\n"
                f"Tiếp tục mua sắm qua link hoàn tiền nha! 🛒"
            )
            await send_text(config.ZALO_BOT_TOKEN, n["zalo_id"], msg)
            await log_notification(n["zalo_id"], "commission", msg)
        except Exception as e:
            logger.error("Notify commission failed for %s: %s", n["zalo_id"], e)

    return {"ok": True, "updated": updated, "notified": len(notified)}
