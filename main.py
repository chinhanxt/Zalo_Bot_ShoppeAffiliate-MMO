"""
Zalo Cashback Bot — MVP (Vercel serverless compatible)
User gửi link Shopee → Bot trả link affiliate (an_redir).
"""

import logging
import re as _re
import json
import os
import hashlib
import secrets
import time

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
import uvicorn

import config
from db import (
    init_db, upsert_user, log_link, get_link_by_code, get_user_stats,
    admin_dashboard_stats, admin_daily_stats, admin_users_list,
    admin_user_detail, admin_links_list, admin_recent_activity, admin_overview_batch,
    get_wallet, create_withdrawal,
    admin_withdrawals_list, admin_process_withdrawal, admin_update_commission,
    update_bank_info, get_bank_info,
    log_notification, notifications_list,
    scheduled_list, scheduled_create, scheduled_toggle, scheduled_delete,
    scheduled_due, scheduled_mark_sent, all_user_chat_ids,
    kv_get, kv_set, kv_get_prefix,
)
from linkgen import extract_shopee_urls, build_affiliate_link, clean_shopee_url
from url_resolver import is_short_url, resolve_redirect
from zalo_api import send_text, send_photo
from ai_chat import ask_gemini, test_gemini_key
from utils import fmt_vnd
from shopee_scraper import (
    get_scraper_status, save_cookies_from_json, save_cookies_from_string,
    verify_cookies, scrape_conversions, sync_commissions_to_wallets,
    _save_state as shopee_save_state,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_db_initialized = False
ADMIN_PASSWORD = config.ADMIN_PASSWORD
ADMIN_SLUG = config.ADMIN_SLUG
_ADM = f"/{ADMIN_SLUG}"
_API_ADM = f"/api/{ADMIN_SLUG}"
_active_sessions: dict[str, bool] = {}


def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


_cfg_loaded_at = 0.0
STR_CFG_KEYS = ["AFFILIATE_ID", "ZALO_BOT_TOKEN", "ZALO_WEBHOOK_SECRET", "GEMINI_API_KEY"]
RATE_CFG_KEYS = ["USER_SHARE_PCT", "COMMISSION_MIN_PCT", "COMMISSION_MAX_PCT", "COMMISSION_CAP_VND"]


async def _ensure_db():
    global _db_initialized, _cfg_loaded_at
    if not _db_initialized:
        await init_db()
        saved_sessions = await kv_get("admin_sessions")
        if saved_sessions:
            for sid in json.loads(saved_sessions):
                _active_sessions[sid] = True
        _db_initialized = True
    if time.time() - _cfg_loaded_at > 60:
        await _load_config_overrides()
        _cfg_loaded_at = time.time()


async def _load_config_overrides():
    kv = await kv_get_prefix("config_")
    for key in STR_CFG_KEYS:
        val = kv.get(f"config_{key}")
        if val:
            setattr(config, key, val)
    for key in RATE_CFG_KEYS:
        val = kv.get(f"config_{key}")
        if val:
            try:
                setattr(config, key, float(val))
            except ValueError:
                pass


def _est_range(amount: float) -> tuple[int, int]:
    share = config.USER_SHARE_PCT / 100
    cap = config.COMMISSION_CAP_VND or float("inf")

    def user_part(pct: float) -> int:
        return int(round(min(amount * pct / 100, cap) * share / 100) * 100)

    lo = user_part(config.COMMISSION_MIN_PCT)
    return lo, max(lo, user_part(config.COMMISSION_MAX_PCT))


def _fmt_est(amount: float) -> str:
    lo, hi = _est_range(amount)
    return fmt_vnd(lo) if hi <= lo else f"{fmt_vnd(lo)} – {fmt_vnd(hi)}"


def _is_authed(request: Request) -> bool:
    sid = request.cookies.get("session_id", "")
    return bool(sid and _active_sessions.get(sid))


@asynccontextmanager
async def lifespan(application: FastAPI):
    await _ensure_db()
    logger.info("Bot ready — AFFILIATE_ID=%s", config.AFFILIATE_ID[:6] + "..." if config.AFFILIATE_ID else "(chưa set)")
    yield


app = FastAPI(title="Zalo Cashback Bot", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def ensure_db_middleware(request: Request, call_next):
    await _ensure_db()
    path = request.url.path
    if path.startswith(_ADM) or path.startswith(_API_ADM):
        if path not in ("/login", "/api/auth/login", "/api/auth/logout"):
            if not _is_authed(request):
                if path.startswith("/api/"):
                    return Response(
                        content=json.dumps({"error": "Unauthorized"}),
                        status_code=401,
                        media_type="application/json",
                    )
                return RedirectResponse("/login")
    return await call_next(request)


# ──────────────────────────────────────────────
# Auth: Login / Logout
# ──────────────────────────────────────────────

LOGIN_HTML = """<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login · Zalo-Bot Shopee</title>
<link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;500;600;700&display=swap&subset=vietnamese" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Be Vietnam Pro",system-ui,sans-serif;background:#F7F6F3;min-height:100vh;display:flex;align-items:center;justify-content:center}
.box{background:#fff;border:1px solid #eaeaea;border-radius:18px;box-shadow:0 1px 2px rgba(17,17,17,.03),0 6px 20px rgba(17,17,17,.035);padding:40px;width:380px;text-align:center}
.logo{width:56px;height:56px;border-radius:16px;background:#EE4D2D;color:#fff;display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:22px;margin-bottom:16px}
h1{font-size:22px;font-weight:700;letter-spacing:-0.02em;margin-bottom:6px}
.sub{font-size:14px;color:#787774;margin-bottom:28px}
input{width:100%;height:48px;border-radius:12px;border:1px solid #eaeaea;padding:0 16px;font:inherit;font-size:15px;outline:none;transition:border .15s}
input:focus{border-color:#111}
.btn{width:100%;height:48px;border-radius:999px;background:#111;color:#fff;border:0;font:inherit;font-size:15px;font-weight:600;cursor:pointer;margin-top:16px}
.btn:hover{background:#333}
.err{color:#B3261E;font-size:13px;margin-top:12px;min-height:18px}
</style></head><body>
<div class="box">
<svg viewBox="0 0 64 64" width="64" height="64" xmlns="http://www.w3.org/2000/svg" style="margin-bottom:16px"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#FF6B35"/><stop offset="100%" stop-color="#EE4D2D"/></linearGradient></defs><rect width="64" height="64" rx="18" fill="url(#lg)"/><text x="32" y="43" text-anchor="middle" font-family="'Be Vietnam Pro',system-ui,sans-serif" font-size="32" font-weight="800" fill="#fff" letter-spacing="-1">xT</text></svg>
<h1>Zalo-Bot Shopee</h1>
<p class="sub">Đăng nhập Affiliate Dashboard</p>
<form onsubmit="return doLogin(event)">
<input type="password" id="pw" placeholder="Mật khẩu" autofocus>
<button class="btn" type="submit">Đăng nhập</button>
</form>
<div class="err" id="err"></div>
</div>
<script>
async function doLogin(e){
  e.preventDefault();
  const pw=document.getElementById('pw').value;
  if(!pw)return;
  const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  const d=await r.json();
  if(d.ok){window.location='/admin';}
  else{document.getElementById('err').textContent=d.error||'Sai mật khẩu';}
}
</script></body></html>"""
LOGIN_HTML = LOGIN_HTML.replace("='/admin'", f"='{_ADM}'")


@app.get("/")
async def root_redirect():
    return RedirectResponse("/login")


@app.get("/login")
async def login_page():
    return HTMLResponse(LOGIN_HTML)


@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    body = await request.json()
    pw = body.get("password", "")
    if _hash_pw(pw) != _hash_pw(ADMIN_PASSWORD):
        return Response(
            content=json.dumps({"ok": False, "error": "Sai mật khẩu"}),
            status_code=401,
            media_type="application/json",
        )
    sid = secrets.token_hex(32)
    _active_sessions[sid] = True
    await kv_set("admin_sessions", json.dumps(list(_active_sessions.keys())[-10:]))
    resp = Response(
        content=json.dumps({"ok": True}),
        media_type="application/json",
    )
    resp.set_cookie("session_id", sid, httponly=True, secure=True, samesite="lax", max_age=86400 * 30)
    return resp


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    sid = request.cookies.get("session_id", "")
    _active_sessions.pop(sid, None)
    await kv_set("admin_sessions", json.dumps(list(_active_sessions.keys())[-10:]))
    resp = RedirectResponse("/login")
    resp.delete_cookie("session_id")
    return resp


# ──────────────────────────────────────────────
# Zalo Bot Webhook (Bot Creator platform)
# ──────────────────────────────────────────────

@app.get("/webhook")
async def webhook_verify(request: Request):
    return {"message": "Success"}


@app.post("/webhook")
async def webhook_handler(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"message": "invalid json"}

    result = body.get("result", body)
    event_name = result.get("event_name", body.get("event_name", ""))

    if event_name == "message.unsupported.received":
        msg = result.get("message", {})
        chat_id = msg.get("chat", {}).get("id", "")
        if chat_id:
            try:
                await _reply_unsupported(chat_id)
            except Exception as e:
                logger.error("unsupported reply failed: %s", e)
    elif event_name.startswith("message.") and event_name.endswith(".received"):
        await handle_user_message(result)
    elif not event_name and result.get("message"):
        await handle_user_message(result)

    return {"message": "Success"}


UNSUPPORTED_TEXT = (
    "📋 Mình chưa đọc được loại tin nhắn này (link kèm card sản phẩm, thẻ ngân hàng, ảnh, sticker...).\n\n"
    "🔗 Gửi link Shopee: dán link → bấm X để xóa card → gửi.\n"
    "🏦 Lưu STK: gõ chữ theo mẫu\n"
    "caidat MBBank 0123456789 NGUYEN VAN A\n\n"
    "Gõ \"help\" để xem các lệnh 😊"
)


async def _reply_unsupported(chat_id: str):
    from datetime import date
    key = f"guide_{date.today().isoformat()}_{chat_id}"
    if not await kv_get(key):
        await kv_set(key, "1")
        res = await send_photo(
            config.ZALO_BOT_TOKEN, chat_id,
            f"https://{config.BOT_DOMAIN}/static/guide-paste-link.png",
            caption=UNSUPPORTED_TEXT,
        )
        if res.get("ok"):
            return
    await send_text(config.ZALO_BOT_TOKEN, chat_id, UNSUPPORTED_TEXT)


import unicodedata as _ud


def _no_accent(s: str) -> str:
    s = _ud.normalize("NFC", s).replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in _ud.normalize("NFD", s) if _ud.category(c) != "Mn")


_BANK_PREFIXES = {
    "caidat", "catdat", "caidt", "cadat", "caiat", "caidatt", "caiddat", "caidar", "caidta",
    "stk", "sotk", "sotaikhoan", "bank", "nganhang", "taikhoan",
}
_KNOWN_BANKS = {
    "vietcombank", "vcb", "techcombank", "tcb", "mb", "mbbank", "mbb", "bidv", "vietinbank", "ctg",
    "agribank", "agr", "acb", "vpbank", "vpb", "tpbank", "tpb", "sacombank", "stb", "hdbank", "hdb",
    "vib", "shb", "msb", "ocb", "seabank", "seab", "eximbank", "eib", "lienvietpostbank", "lpbank", "lpb",
    "namabank", "nab", "bacabank", "bab", "pvcombank", "vietabank", "abbank", "kienlongbank", "klb",
    "ncb", "saigonbank", "sgb", "baovietbank", "bvb", "scb", "gpbank", "oceanbank", "vietbank", "vbb",
    "cake", "timo", "ubank", "tnex", "liobank", "momo", "zalopay", "viettelmoney", "vnpay",
    "shinhan", "shinhanbank", "woori", "uob", "hsbc", "cimb", "publicbank", "indovina", "vrb", "vikki",
}
_ACC_TOKEN_RE = _re.compile(r"[\d.\-]+")


def _is_known_bank(name: str) -> bool:
    k = _no_accent(name).lower().replace(" ", "").replace("-", "")
    return k in _KNOWN_BANKS or (k.endswith("bank") and k.isalpha() and len(k) > 4)


def _parse_bank_info(text: str):
    """None = không phải lệnh STK; "incomplete" = có ý lưu STK nhưng sai mẫu; dict = hợp lệ."""
    tokens = text.split()
    norm = [_no_accent(t).lower().strip(":,.-") for t in tokens]
    skip = 0
    if len(norm) >= 3 and "".join(norm[:3]) in _BANK_PREFIXES:
        skip = 3
    elif len(norm) >= 2 and "".join(norm[:2]) in _BANK_PREFIXES:
        skip = 2
    elif norm and norm[0] in _BANK_PREFIXES:
        skip = 1
    has_prefix = skip > 0
    rest = tokens[skip:]

    acc_start = acc_end = None
    for i, t in enumerate(rest):
        tt = t.strip(":,")
        if not (_ACC_TOKEN_RE.fullmatch(tt) and any(c.isdigit() for c in tt)):
            continue
        j = i + 1
        while j < len(rest) and _ACC_TOKEN_RE.fullmatch(rest[j].strip(":,")):
            j += 1
        if sum(c.isdigit() for c in "".join(rest[i:j])) >= 6:
            acc_start, acc_end = i, j
            break
    if acc_start is None:
        return "incomplete" if has_prefix else None

    account = "".join(c for c in "".join(rest[acc_start:acc_end]) if c.isdigit())
    bank_tokens = rest[:acc_start]
    holder_tokens = rest[acc_end:]

    if not bank_tokens and holder_tokens and _is_known_bank(holder_tokens[0]):
        bank_tokens, holder_tokens = holder_tokens[:1], holder_tokens[1:]
    elif bank_tokens and not _is_known_bank(" ".join(bank_tokens)) and holder_tokens and _is_known_bank(holder_tokens[0]):
        bank_tokens, holder_tokens = holder_tokens[:1], holder_tokens[1:]

    bank = " ".join(t.strip(":,") for t in bank_tokens).strip()
    holder = " ".join(t.strip(":,") for t in holder_tokens).strip()

    bank_ok = bool(bank) and (_is_known_bank(bank) or (has_prefix and len(bank_tokens) == 1 and bank.replace("-", "").isalpha()))
    holder_ok = bool(holder) and not any(c.isdigit() for c in holder) and any(c.isalpha() for c in holder)
    acc_ok = 6 <= len(account) <= 20

    if not (bank_ok and holder_ok and acc_ok):
        mentions_bank = any(n in _BANK_PREFIXES or n in _KNOWN_BANKS or _is_known_bank(n) for n in norm)
        if has_prefix or mentions_bank:
            return "incomplete"
        return None
    return {"bank_name": bank, "bank_account": account, "bank_holder": _no_accent(holder).upper()}


BANK_TEMPLATE = (
    "🏦 Lưu tài khoản ngân hàng\n"
    "━━━━━━━━━━━━━━━\n"
    "Gửi đúng 1 dòng theo mẫu:\n"
    "caidat [ngân hàng] [số TK] [tên chủ TK]\n\n"
    "Ví dụ:\n"
    "caidat MBBank 0123456789 NGUYEN VAN A\n"
    "caidat Vietcombank 1234567890 TRAN THI B\n\n"
    "⚠️ Gõ bằng chữ, không gửi thẻ/ảnh ngân hàng nhé!"
)


def _fmt_vnd(amount: float) -> str:
    return fmt_vnd(amount)



def _parse_vnd(text: str) -> float | None:
    s = text.lower().strip()
    s = _re.sub(r'[đ₫vndd\s]', '', s)
    if not s:
        return None
    m = _re.match(r'^(\d+[.,]?\d*)\s*tr(\d+)?$', s)
    if m:
        base = float(m.group(1).replace(',', '.')) * 1_000_000
        frac = int(m.group(2)) * 100_000 if m.group(2) else 0
        return base + frac
    m = _re.match(r'^(\d+[.,]?\d*)\s*trieu$', s)
    if m:
        return float(m.group(1).replace(',', '.')) * 1_000_000
    m = _re.match(r'^(\d+[.,]?\d*)\s*k$', s)
    if m:
        return float(m.group(1).replace(',', '.')) * 1_000
    m = _re.match(r'^(\d+[.,]?\d*)\s*(ngan|nghin|ngàn|nghìn)$', s)
    if m:
        return float(m.group(1).replace(',', '.')) * 1_000
    m = _re.match(r'^[\d.,]+$', s)
    if m:
        cleaned = s.replace('.', '').replace(',', '')
        if cleaned.isdigit() and int(cleaned) >= 1000:
            return float(cleaned)
    return None




async def handle_user_message(result: dict):
    msg = result.get("message", {})
    sender = msg.get("from", {})
    chat = msg.get("chat", {})

    user_id = sender.get("id", "")
    chat_id = chat.get("id", "")
    display_name = sender.get("display_name", "")
    text = (msg.get("text") or "").strip()

    if not text:
        links = msg.get("links", [])
        if isinstance(links, list):
            for lk in links:
                u = lk.get("url", "") if isinstance(lk, dict) else ""
                if u:
                    text = u
                    break
        if not text:
            text = (msg.get("url") or "").strip()
        if not text:
            for att in msg.get("attachments", []):
                if isinstance(att, dict):
                    payload = att.get("payload", {})
                    u = payload.get("url", "") if isinstance(payload, dict) else ""
                    if u:
                        text = u
                        break

    if not chat_id or not text:
        return

    tracking_id = user_id or chat_id
    cmd = text.lower().strip()

    if cmd in ("5", "help", "menu", "huong dan", "hướng dẫn", "huong dan", "start", "hi", "xin chao", "xin chào"):
        await send_text(
            config.ZALO_BOT_TOKEN, chat_id,
            f"Chào {display_name or 'bạn'}! 👋\n\n"
            f"📝 Lệnh nhanh (bấm số hoặc gõ tên):\n"
            f"1 · Ví — xem số dư 👛\n"
            f"2 · Cài đặt — lưu STK 🏦\n"
            f"3 · Rút tiền — rút về TK 💸\n"
            f"4 · Thống kê — xem lịch sử 📊\n"
            f"5 · Hướng dẫn 📖\n"
            f"6 · Tính nhận tiền — VD: 6 200k 🧮\n"
            f"🔗 Gửi link Shopee → nhận link nhận tiền\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🛒 Cách nhận tiền:\n"
            f"Gửi link Shopee → mua qua link nhận tiền → đơn thành công → Shopee xác nhận (~7-30 ngày) → tiền vào ví → rút về ngân hàng!\n\n"
            f"💰 VD: đơn 200.000đ → nhận khoảng {_fmt_est(200000)}\n"
            f"Gõ \"6 [giá đơn]\" để tính chính xác hơn 🧮"
        )
        return

    tinh_match = _re.match(r'^(?:6|tinh|tính|tính tiền|tinhtien)\s+(.+)$', cmd)
    if tinh_match:
        amount = _parse_vnd(tinh_match.group(1))
        if amount and amount >= 1000:
            await send_text(
                config.ZALO_BOT_TOKEN, chat_id,
                f"🧮 Ước tính nhận tiền\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🛒 Giá đơn: {_fmt_vnd(amount)}\n"
                f"💰 Nhận khoảng: {_fmt_est(amount)}\n\n"
                f"📌 Số tiền tạm tính, mức thật tuỳ ngành hàng & shop. Đơn thành công → tiền tự vào ví!\n"
                f"Gửi link Shopee để nhận link nhận tiền nha 🛒"
            )
        else:
            await send_text(
                config.ZALO_BOT_TOKEN, chat_id,
                f"🤔 Mình không hiểu số tiền. Thử lại nhé!\n"
                f"VD: 6 200k · 6 1tr5 · 6 500.000"
            )
        return

    if cmd in ("6", "tinh", "tính", "tính tiền", "tinhtien"):
        await send_text(
            config.ZALO_BOT_TOKEN, chat_id,
            f"🧮 Tính nhận tiền\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Nhập giá đơn hàng để tính:\n\n"
            f"VD:\n"
            f"• 6 200k → nhận khoảng {_fmt_est(200000)}\n"
            f"• 6 1tr5 → nhận khoảng {_fmt_est(1500000)}\n"
            f"• 6 500.000 → nhận khoảng {_fmt_est(500000)}\n\n"
            f"💡 Gõ: 6 [giá đơn]"
        )
        return

    digits_only = text.replace(" ", "").replace(".", "")
    looks_like_account = digits_only.isdigit() and digits_only.startswith("0") and len(digits_only) >= 9
    bare_amount = None if looks_like_account else _parse_vnd(text)
    if bare_amount and 1000 <= bare_amount <= 1_000_000_000:
        await send_text(
            config.ZALO_BOT_TOKEN, chat_id,
            f"🧮 Ước tính nhận tiền\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🛒 Giá đơn: {_fmt_vnd(bare_amount)}\n"
            f"💰 Nhận khoảng: {_fmt_est(bare_amount)}\n\n"
            f"📌 Số tiền tạm tính, mức thật tuỳ ngành hàng & shop. Đơn thành công → tiền tự vào ví!\n"
            f"Gửi link Shopee để nhận link nhận tiền nha 🛒"
        )
        return

    await upsert_user(tracking_id, display_name)

    if cmd in ("1", "vi", "ví", "wallet", "sodu", "số dư", "so du"):
        w = await get_wallet(tracking_id)
        reply = (
            f"👛 Ví của bạn\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 Số dư: {_fmt_vnd(w['balance'])}\n"
            f"📦 Tổng kiếm: {_fmt_vnd(w['total_earned'])}\n"
            f"💸 Đã rút: {_fmt_vnd(w['total_withdrawn'])}\n"
            f"🔗 Link đã tạo: {w['total_clicks']}\n"
            f"⏳ Chờ xác nhận: {w['pending_links']} link\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📌 Tiền vào ví khi nào?\n"
            f"Mua qua link → đơn giao thành công → Shopee xác nhận hoa hồng (7-30 ngày) → tiền tự vào ví!\n\n"
            f"Gõ \"caidat\" lưu STK, \"ruttien\" rút tiền 🎉"
        )
        await send_text(config.ZALO_BOT_TOKEN, chat_id, reply)
        return

    ncmd = _no_accent(cmd)
    if ncmd.replace(" ", "") in ("2", "caidat", "catdat", "caidt", "cadat", "setting", "stk", "sotk",
                                 "nganhang", "taikhoan", "bank"):
        bank = await get_bank_info(tracking_id)
        if bank["bank_name"]:
            reply = (
                f"🏦 Tài khoản ngân hàng đã lưu:\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🏧 Ngân hàng: {bank['bank_name']}\n"
                f"💳 Số TK: {bank['bank_account']}\n"
                f"👤 Chủ TK: {bank['bank_holder']}\n"
                f"━━━━━━━━━━━━━━━\n\n"
                f"Muốn đổi? Gửi lại theo mẫu:\n"
                f"caidat [ngân hàng] [số TK] [tên chủ TK]\n\n"
                f"Ví dụ: caidat Vietcombank 1234567890 Nguyen Van A"
            )
        else:
            reply = (
                f"🏦 Cài đặt tài khoản ngân hàng\n"
                f"━━━━━━━━━━━━━━━\n"
                f"Bạn chưa lưu thông tin ngân hàng.\n\n"
                f"Gửi theo mẫu:\n"
                f"caidat [ngân hàng] [số TK] [tên chủ TK]\n\n"
                f"Ví dụ: caidat Vietcombank 1234567890 Nguyen Van A\n\n"
                f"💡 Lưu 1 lần, rút tiền chỉ cần gõ số tiền thôi! 😊"
            )
        await send_text(config.ZALO_BOT_TOKEN, chat_id, reply)
        return

    bank_parsed = _parse_bank_info(text)
    if bank_parsed == "incomplete":
        await send_text(config.ZALO_BOT_TOKEN, chat_id, BANK_TEMPLATE)
        return
    if bank_parsed:
        await update_bank_info(tracking_id, bank_parsed["bank_name"],
                               bank_parsed["bank_account"], bank_parsed["bank_holder"])
        saved = await get_bank_info(tracking_id)
        if saved.get("bank_account") != bank_parsed["bank_account"]:
            logger.error("bank save mismatch for %s: %s", tracking_id, saved)
            await send_text(config.ZALO_BOT_TOKEN, chat_id,
                "😢 Lưu STK chưa thành công, bạn gửi lại giúp mình nhé!\n\n" + BANK_TEMPLATE)
            return
        await send_text(
            config.ZALO_BOT_TOKEN, chat_id,
            f"✅ Đã lưu thông tin ngân hàng!\n\n"
            f"🏧 Ngân hàng: {saved['bank_name']}\n"
            f"💳 Số TK: {saved['bank_account']}\n"
            f"👤 Chủ TK: {saved['bank_holder']}\n\n"
            f"Sai thông tin? Gửi lại lệnh caidat để sửa.\n"
            f"Rút tiền: gõ rut [số tiền] — VD: rut 50000 🎉"
        )
        return

    if cmd in ("3", "ruttien", "rút tiền", "rut tien", "withdraw"):
        w = await get_wallet(tracking_id)
        bank = await get_bank_info(tracking_id)
        problems = []
        if not bank["bank_name"]:
            problems.append("🏦 Chưa cài đặt tài khoản ngân hàng\n→ Gõ \"caidat\" để lưu STK nhé!")
        if w["balance"] < 20000:
            problems.append(f"💰 Số dư: {_fmt_vnd(w['balance'])} — chưa đủ 20.000đ tối thiểu\n→ Chia sẻ thêm link Shopee để tích lũy nha!")

        if problems:
            await send_text(
                config.ZALO_BOT_TOKEN, chat_id,
                f"😅 Chưa thể rút tiền nha!\n"
                f"━━━━━━━━━━━━━━━\n\n" +
                "\n\n".join(problems) +
                f"\n\n📌 Điều kiện rút tiền:\n"
                f"• Số dư tối thiểu: 20.000đ\n"
                f"• Đã cài đặt STK (gõ \"caidat\")"
            )
            return

        await send_text(
            config.ZALO_BOT_TOKEN, chat_id,
            f"💸 Rút tiền\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 Số dư: {_fmt_vnd(w['balance'])}\n"
            f"🏦 TK: {bank['bank_name']} - {bank['bank_account']} - {bank['bank_holder']}\n\n"
            f"Gõ số tiền muốn rút:\n"
            f"rut [số tiền]\n\n"
            f"Ví dụ: rut 50000\n"
            f"Rút hết: rut {int(w['balance'])}"
        )
        return

    if ncmd.startswith("rut ") and len(ncmd.split()) >= 2:
        arg = ncmd.split(None, 1)[1].strip()
        if arg.startswith("tien "):
            arg = arg[5:].strip()
        if arg in ("het", "tat ca", "all"):
            amount = float((await get_wallet(tracking_id))["balance"])
        else:
            amount = _parse_vnd(arg)
            if amount is None:
                try:
                    amount = float(arg.replace(",", "").replace(".", "").replace("d", "").strip())
                except ValueError:
                    amount = None
        if amount is None:
            await send_text(config.ZALO_BOT_TOKEN, chat_id,
                "🤔 Mình không hiểu số tiền. Thử lại nhé!\n"
                "Ví dụ: rut 50000 · rut 50k · rut het")
            return

        if amount < 20000:
            await send_text(config.ZALO_BOT_TOKEN, chat_id,
                f"😅 Số tiền tối thiểu để rút là 20.000đ nha!\n"
                f"Bạn đang muốn rút {_fmt_vnd(amount)}.")
            return

        w = await get_wallet(tracking_id)
        if w["balance"] < amount:
            await send_text(config.ZALO_BOT_TOKEN, chat_id,
                f"😢 Số dư không đủ!\n"
                f"Số dư hiện tại: {_fmt_vnd(w['balance'])}\n"
                f"Bạn muốn rút: {_fmt_vnd(amount)}")
            return

        bank = await get_bank_info(tracking_id)
        if not bank["bank_name"]:
            await send_text(config.ZALO_BOT_TOKEN, chat_id,
                "🏦 Bạn chưa cài đặt tài khoản ngân hàng.\n"
                "Gõ \"caidat\" để lưu STK trước khi rút nhé!")
            return

        bank_info = f"{bank['bank_name']} {bank['bank_account']} {bank['bank_holder']}"
        result_w = await create_withdrawal(tracking_id, amount, bank_info)
        if not result_w["ok"]:
            await send_text(config.ZALO_BOT_TOKEN, chat_id,
                f"😢 Lỗi: {result_w['error']}")
            return
        await send_text(
            config.ZALO_BOT_TOKEN, chat_id,
            f"✅ Đã gửi yêu cầu rút tiền!\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"📋 Mã yêu cầu: #{result_w['id']}\n"
            f"💰 Số tiền: {_fmt_vnd(amount)}\n"
            f"🏦 Ngân hàng: {bank['bank_name']}\n"
            f"💳 STK: {bank['bank_account']}\n"
            f"👤 Chủ TK: {bank['bank_holder']}\n\n"
            f"⏳ Admin sẽ xác minh và chuyển khoản trong 1-3 ngày làm việc.\n"
            f"Mình sẽ thông báo khi có kết quả nhé! 🥰"
        )
        return

    if cmd in ("4", "thongke", "thống kê", "thong ke", "stats", "lịch sử", "lich su"):
        stats = await get_user_stats(tracking_id)
        w = await get_wallet(tracking_id)
        reply = (
            f"📊 Thống kê của bạn\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 Số dư: {_fmt_vnd(w['balance'])}\n"
            f"📦 Tổng kiếm: {_fmt_vnd(w['total_earned'])}\n"
            f"🔗 Tổng link: {stats['total_clicks']}\n"
        )
        if stats["recent_links"]:
            reply += "\n📋 5 link gần nhất:\n"
            for i, lk in enumerate(stats["recent_links"], 1):
                reply += f"{i}. {lk['original_url'][:45]}...\n"
        else:
            reply += "\nChưa có link nào — gửi link Shopee để bắt đầu nhé! 🛒"
        await send_text(config.ZALO_BOT_TOKEN, chat_id, reply)
        return

    if not config.AFFILIATE_ID:
        await send_text(config.ZALO_BOT_TOKEN, chat_id,
            "😅 Bot đang bảo trì, bạn quay lại sau nhé!")
        return

    urls = extract_shopee_urls(text)
    if not urls:
        ai_reply = None
        if config.GEMINI_API_KEY:
            from datetime import date
            ai_limit = int(await kv_get("config_AI_USER_LIMIT") or "10")
            usage_key = f"ai_{date.today().isoformat()}_{tracking_id}"
            usage_raw = await kv_get(usage_key)
            ai_count = int(usage_raw) if usage_raw else 0
            if ai_count >= ai_limit:
                await send_text(
                    config.ZALO_BOT_TOKEN, chat_id,
                    "Gửi link sản phẩm Shopee cho mình để nhận nhận tiền nhé! 🛒\n"
                    "Gõ \"help\" để xem hướng dẫn 👋",
                )
                return
            ai_reply = await ask_gemini(config.GEMINI_API_KEY, text, display_name)
            if ai_reply:
                await kv_set(usage_key, str(ai_count + 1))
        if ai_reply:
            await send_text(config.ZALO_BOT_TOKEN, chat_id, ai_reply)
        else:
            await send_text(
                config.ZALO_BOT_TOKEN, chat_id,
                f"🤔 Mình không thấy link Shopee nha!\n"
                f"Gửi link shopee.vn hoặc shp.ee để nhận nhận tiền 🛒\n"
                f"Gõ \"help\" để xem hướng dẫn 😊"
            )
        return

    results: list[str] = []
    for url in urls:
        full_url = url
        if is_short_url(url):
            full_url = await resolve_redirect(url)
        full_url = clean_shopee_url(full_url)

        aff_link = build_affiliate_link(
            product_url=full_url,
            affiliate_id=config.AFFILIATE_ID,
            sub_ids=[tracking_id],
        )

        code = await log_link(tracking_id, full_url, aff_link, display_name)
        short_url = f"https://{config.BOT_DOMAIN}/s/{code}"
        results.append(short_url)

    if len(results) == 1:
        reply = (
            f"🎉 Link nhận tiền của bạn:\n\n"
            f"🔗 {results[0]}\n\n"
            f"💰 VD: đơn 200.000đ → nhận khoảng {_fmt_est(200000)}\n"
            f"📌 Mua qua link → đơn thành công → tiền tự vào ví!\n\n"
            f"💡 Gõ \"6 [giá đơn]\" để tính nhận tiền\n"
            f"VD: 6 200k · 6 1tr5 · 6 500.000"
        )
    else:
        reply = f"🎉 {len(results)} link nhận tiền:\n\n"
        for i, lk in enumerate(results, 1):
            reply += f"{i}. {lk}\n"
        reply += (
            f"\n💰 VD: đơn 200.000đ → nhận khoảng {_fmt_est(200000)} · Đơn thành công → tiền vào ví!\n\n"
            f"💡 Gõ \"6 [giá đơn]\" để tính nhận tiền"
        )

    await send_text(config.ZALO_BOT_TOKEN, chat_id, reply)


# ──────────────────────────────────────────────
# Short link redirect
# ──────────────────────────────────────────────

@app.get("/static/{filename}")
async def public_static(filename: str):
    fp = os.path.join(ADMIN_DIR, filename)
    if os.path.isfile(fp) and filename.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return FileResponse(fp)
    return Response(status_code=404)


@app.get("/s/{code}")
async def short_redirect(code: str):
    link = await get_link_by_code(code)
    if not link:
        return HTMLResponse("<h3>Link không tồn tại hoặc đã hết hạn</h3>", status_code=404)
    return RedirectResponse(link["affiliate_url"], status_code=302)


# ──────────────────────────────────────────────
# Health & manual test
# ──────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "affiliate_id_set": bool(config.AFFILIATE_ID)}


@app.get("/test-link")
async def test_link(url: str):
    if not config.AFFILIATE_ID:
        return {"error": "AFFILIATE_ID chưa set"}
    urls = extract_shopee_urls(url)
    if not urls:
        return {"error": "Không phải URL Shopee"}
    full_url = urls[0]
    if is_short_url(full_url):
        full_url = await resolve_redirect(full_url)
    aff = build_affiliate_link(full_url, config.AFFILIATE_ID, sub_ids=["TEST"])
    return {"original": full_url, "affiliate": aff}


# ──────────────────────────────────────────────
# Admin Panel
# ──────────────────────────────────────────────

ADMIN_DIR = os.path.join(os.path.dirname(__file__), "admin")


@app.get(_ADM)
async def admin_page():
    with open(os.path.join(ADMIN_DIR, "index.html")) as f:
        html = f.read().replace("/api/admin", _API_ADM)
    return HTMLResponse(html)


@app.get(_ADM + "/{path:path}")
async def admin_static(path: str):
    fp = os.path.join(ADMIN_DIR, path)
    if os.path.isfile(fp):
        return FileResponse(fp)
    with open(os.path.join(ADMIN_DIR, "index.html")) as f:
        html = f.read().replace("/api/admin", _API_ADM)
    return HTMLResponse(html)


@app.get(_API_ADM + "/overview")
async def api_admin_overview():
    data = await admin_overview_batch()
    data["stats"]["affiliate_id_set"] = bool(config.AFFILIATE_ID)
    data["shopee"] = await get_scraper_status()
    data["config"] = {
        "affiliate_id_set": bool(config.AFFILIATE_ID),
        "zalo_bot_token_set": bool(config.ZALO_BOT_TOKEN),
        "gemini_api_key_set": bool(config.GEMINI_API_KEY),
    }
    return data


@app.get(_API_ADM + "/stats")
async def api_admin_stats():
    stats = await admin_dashboard_stats()
    stats["affiliate_id_set"] = bool(config.AFFILIATE_ID)
    return stats


@app.get(_API_ADM + "/daily-stats")
async def api_admin_daily(days: int = Query(default=7, ge=1, le=90)):
    return await admin_daily_stats(days)


@app.get(_API_ADM + "/users")
async def api_admin_users(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    search: str = "",
    sort: str = "links_desc",
):
    return await admin_users_list(page, per_page, search, sort)


@app.get(_API_ADM + "/users/{zalo_id}")
async def api_admin_user_detail(zalo_id: str):
    user = await admin_user_detail(zalo_id)
    if not user:
        return {"error": "User not found"}
    return user


@app.get(_API_ADM + "/links")
async def api_admin_links(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    search: str = "",
    user: str = "",
):
    return await admin_links_list(page, per_page, search, user)


@app.get(_API_ADM + "/recent")
async def api_admin_recent(limit: int = Query(default=5, ge=1, le=20)):
    return await admin_recent_activity(limit)


@app.get(_API_ADM + "/config")
async def api_admin_config():
    aff = config.AFFILIATE_ID
    token = config.ZALO_BOT_TOKEN
    secret = config.ZALO_WEBHOOK_SECRET
    gemini = config.GEMINI_API_KEY
    return {
        "affiliate_id": aff,
        "affiliate_id_set": bool(aff),
        "zalo_bot_token": token,
        "zalo_bot_token_set": bool(token),
        "zalo_bot_token_preview": (token[:12] + "…" + token[-4:]) if len(token) > 16 else ("••••" if token else ""),
        "zalo_webhook_secret": secret,
        "zalo_webhook_secret_set": bool(secret),
        "gemini_api_key": gemini,
        "gemini_api_key_set": bool(gemini),
        "ai_user_limit": int(await kv_get("config_AI_USER_LIMIT") or "10"),
        "user_share_pct": config.USER_SHARE_PCT,
        "commission_min_pct": config.COMMISSION_MIN_PCT,
        "commission_max_pct": config.COMMISSION_MAX_PCT,
        "commission_cap_vnd": config.COMMISSION_CAP_VND,
        "host": config.HOST,
        "port": config.PORT,
    }


ENV_KEYS = {"AFFILIATE_ID", "ZALO_BOT_TOKEN", "ZALO_WEBHOOK_SECRET", "GEMINI_API_KEY"}


@app.post(_API_ADM + "/config")
async def api_admin_config_update(request: Request):
    body = await request.json()
    updated = []
    rates = {}
    for key in RATE_CFG_KEYS:
        if key in body and str(body[key]).strip() != "":
            try:
                rates[key] = float(body[key])
            except (TypeError, ValueError):
                return {"ok": False, "error": f"{key} phải là số"}
    share = rates.get("USER_SHARE_PCT", config.USER_SHARE_PCT)
    cmin = rates.get("COMMISSION_MIN_PCT", config.COMMISSION_MIN_PCT)
    cmax = rates.get("COMMISSION_MAX_PCT", config.COMMISSION_MAX_PCT)
    if not (0 < share <= 100):
        return {"ok": False, "error": "% user nhận phải trong khoảng 1–100"}
    if not (0 < cmin <= 100 and 0 < cmax <= 100):
        return {"ok": False, "error": "% hoa hồng Shopee phải trong khoảng 0–100"}
    if cmax < cmin:
        return {"ok": False, "error": "Hoa hồng cao nhất phải ≥ thấp nhất"}
    if rates.get("COMMISSION_CAP_VND", 0) < 0:
        return {"ok": False, "error": "Trần hoa hồng không được âm (0 = không giới hạn)"}
    for key, val in rates.items():
        setattr(config, key, val)
        await kv_set(f"config_{key}", str(val))
        updated.append(key)
    for key in ENV_KEYS:
        if key in body:
            val = str(body[key]).strip()
            setattr(config, key, val)
            await kv_set(f"config_{key}", val)
            updated.append(key)
    if "AI_USER_LIMIT" in body:
        val = str(int(body["AI_USER_LIMIT"]))
        await kv_set("config_AI_USER_LIMIT", val)
        updated.append("AI_USER_LIMIT")
    return {"ok": True, "updated": updated}


@app.post(_API_ADM + "/test-gemini")
async def api_admin_test_gemini():
    return await test_gemini_key(config.GEMINI_API_KEY)


@app.get(_API_ADM + "/withdrawals")
async def api_admin_withdrawals(page: int = 1, per_page: int = 10, status: str = ""):
    return await admin_withdrawals_list(page, per_page, status)


@app.post(_API_ADM + "/withdrawals/{wid}/process")
async def api_admin_process_withdrawal(wid: int, request: Request):
    body = await request.json()
    action = body.get("action", "")
    note = body.get("note", "")

    result = await admin_process_withdrawal(wid, action, note)
    w_info = result.get("withdrawal")
    if result.get("ok") and w_info:
        zalo_id = w_info["zalo_id"]
        amount = w_info["amount"]
        if action == "approve":
            msg = (
                f"🎉 Yêu cầu rút tiền #{wid} đã được DUYỆT!\n\n"
                f"💰 Số tiền: {_fmt_vnd(amount)}\n"
                f"🏦 Chuyển tới: {w_info['bank_info']}\n\n"
                f"Admin sẽ chuyển khoản sớm nhé! 😊"
            )
        else:
            msg = (
                f"😔 Yêu cầu rút tiền #{wid} bị từ chối.\n\n"
                f"💰 Số tiền: {_fmt_vnd(amount)} đã trả về ví.\n"
                f"📝 Lý do: {note or 'Không có'}\n\n"
                f"Liên hệ admin nếu cần hỗ trợ nhé!"
            )
        try:
            await send_text(config.ZALO_BOT_TOKEN, zalo_id, msg)
            await log_notification(zalo_id, "withdrawal", msg)
        except Exception as e:
            logger.error("Notify user failed: %s", e)

    return result


@app.post(_API_ADM + "/links/{link_id}/commission")
async def api_admin_set_commission(link_id: int, request: Request):
    body = await request.json()
    return await admin_update_commission(link_id, float(body.get("commission_user", 0)))


# ──────────────────────────────────────────────
# Notifications
# ──────────────────────────────────────────────

@app.get(_API_ADM + "/notifications")
async def api_admin_notifications(page: int = 1, per_page: int = 10):
    return await notifications_list(page, per_page)


@app.post(_API_ADM + "/notifications/send")
async def api_admin_send_notification(request: Request):
    body = await request.json()
    message = body.get("message", "").strip()
    zalo_id = body.get("zalo_id", "").strip()
    if not message:
        return {"ok": False, "error": "Tin nhắn trống"}

    if zalo_id:
        try:
            await send_text(config.ZALO_BOT_TOKEN, zalo_id, message)
            await log_notification(zalo_id, "custom", message, 1)
            return {"ok": True, "sent": 1}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    else:
        chat_ids = await all_user_chat_ids()
        sent = 0
        for cid in chat_ids:
            try:
                await send_text(config.ZALO_BOT_TOKEN, cid, message)
                sent += 1
            except Exception:
                pass
        await log_notification("", "broadcast", message, sent)
        return {"ok": True, "sent": sent}


# ──────────────────────────────────────────────
# Scheduled messages
# ──────────────────────────────────────────────

@app.get(_API_ADM + "/scheduled")
async def api_admin_scheduled():
    return await scheduled_list()


@app.post(_API_ADM + "/scheduled")
async def api_admin_scheduled_create(request: Request):
    body = await request.json()
    message = body.get("message", "").strip()
    interval = int(body.get("interval_hours", 24))
    if not message:
        return {"ok": False, "error": "Tin nhắn trống"}
    sid = await scheduled_create(message, interval)
    return {"ok": True, "id": sid}


@app.post(_API_ADM + "/scheduled/{sid}/toggle")
async def api_admin_scheduled_toggle(sid: int, request: Request):
    body = await request.json()
    return await scheduled_toggle(sid, int(body.get("is_active", 0)))


@app.delete(_API_ADM + "/scheduled/{sid}")
async def api_admin_scheduled_del(sid: int):
    return await scheduled_delete(sid)


# ──────────────────────────────────────────────
# Shopee Affiliate Scraper
# ──────────────────────────────────────────────

@app.get(_API_ADM + "/shopee/status")
async def api_shopee_status():
    return await get_scraper_status()


@app.post(_API_ADM + "/shopee/cookies")
async def api_shopee_cookies(request: Request):
    body = await request.json()
    if isinstance(body, list):
        return await save_cookies_from_json(body)
    cookie_str = body.get("cookie_string", "")
    if cookie_str:
        return await save_cookies_from_string(cookie_str)
    cookies = body.get("cookies", [])
    if cookies:
        return await save_cookies_from_json(cookies)
    return {"ok": False, "error": "Gửi JSON array cookies hoặc {cookie_string: '...'}"}


@app.post(_API_ADM + "/shopee/verify")
async def api_shopee_verify():
    return await verify_cookies()


@app.post(_API_ADM + "/shopee/sync")
async def api_shopee_sync():
    result = await scrape_conversions(days=30)
    if not result.get("ok"):
        return result
    if result.get("conversions"):
        sync = await sync_commissions_to_wallets(result["conversions"])
        result["wallet_sync"] = sync
    return result


@app.post(_API_ADM + "/shopee/interval")
async def api_shopee_interval(request: Request):
    body = await request.json()
    hours = int(body.get("hours", 6))
    hours = max(1, min(hours, 168))
    await shopee_save_state({"sync_interval_hours": hours})
    return {"ok": True, "interval_hours": hours}


# ──────────────────────────────────────────────
# Cron endpoints (gọi bởi cron-job.org bên ngoài)
# ──────────────────────────────────────────────

def _check_cron_secret(request: Request) -> bool:
    if not config.CRON_SECRET:
        return True
    return request.headers.get("x-cron-secret", "") == config.CRON_SECRET


@app.post("/api/cron/scheduler")
async def cron_scheduler(request: Request):
    if not _check_cron_secret(request):
        return Response(status_code=401)
    due = await scheduled_due()
    if not due:
        return {"ok": True, "processed": 0}
    chat_ids = await all_user_chat_ids()
    processed = 0
    for sch in due:
        sent = 0
        for cid in chat_ids:
            try:
                await send_text(config.ZALO_BOT_TOKEN, cid, sch["message"])
                sent += 1
            except Exception:
                pass
        await scheduled_mark_sent(sch["id"])
        await log_notification("", "scheduled", sch["message"], sent)
        processed += 1
    return {"ok": True, "processed": processed}


@app.post("/api/cron/shopee-sync")
async def cron_shopee_sync(request: Request):
    if not _check_cron_secret(request):
        return Response(status_code=401)
    status = await get_scraper_status()
    if not status["has_cookies"]:
        return {"ok": False, "reason": "no_cookies"}
    result = await scrape_conversions(days=7)
    if not result.get("ok"):
        if "hết hạn" in result.get("error", ""):
            try:
                admin_ids = await all_user_chat_ids()
                if admin_ids:
                    await send_text(
                        config.ZALO_BOT_TOKEN, admin_ids[0],
                        "⚠️ [Admin] Cookie Shopee đã hết hạn!\n"
                        "Vào Admin → Shopee Sync → Upload cookies mới để tiếp tục đồng bộ hoa hồng.",
                    )
            except Exception:
                pass
        return result
    if result.get("conversions"):
        sync = await sync_commissions_to_wallets(result["conversions"])
        result["wallet_sync"] = sync
    return result


if __name__ == "__main__":
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
