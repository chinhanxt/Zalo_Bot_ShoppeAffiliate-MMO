"""
Zalo Cashback Bot — MVP (Vercel serverless compatible)
User gửi link Shopee → Bot trả link affiliate (an_redir).
"""

import logging
import json
import os
import hashlib
import secrets

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
import uvicorn

import config
from db import (
    init_db, upsert_user, log_link, get_user_stats,
    admin_dashboard_stats, admin_daily_stats, admin_users_list,
    admin_user_detail, admin_links_list, admin_recent_activity, admin_overview_batch,
    get_wallet, create_withdrawal,
    admin_withdrawals_list, admin_process_withdrawal, admin_update_commission,
    update_bank_info, get_bank_info,
    log_notification, notifications_list,
    scheduled_list, scheduled_create, scheduled_toggle, scheduled_delete,
    scheduled_due, scheduled_mark_sent, all_user_chat_ids,
    kv_get, kv_set,
)
from linkgen import extract_shopee_urls, build_affiliate_link
from url_resolver import is_short_url, resolve_redirect
from zalo_api import send_text
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


async def _ensure_db():
    global _db_initialized
    if not _db_initialized:
        await init_db()
        await _load_config_overrides()
        _db_initialized = True


async def _load_config_overrides():
    for key in ["AFFILIATE_ID", "ZALO_BOT_TOKEN", "ZALO_WEBHOOK_SECRET", "GEMINI_API_KEY"]:
        val = await kv_get(f"config_{key}")
        if val is not None and val != "":
            setattr(config, key, val)
    saved_sessions = await kv_get("admin_sessions")
    if saved_sessions:
        for sid in json.loads(saved_sessions):
            _active_sessions[sid] = True


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
    if config.ZALO_WEBHOOK_SECRET:
        secret = request.headers.get("x-bot-api-secret-token", "")
        if secret != config.ZALO_WEBHOOK_SECRET:
            logger.warning("Invalid webhook secret")
            return Response(
                content=json.dumps({"message": "Unauthorized"}),
                status_code=403,
                media_type="application/json",
            )

    try:
        body = await request.json()
    except Exception:
        return {"message": "invalid json"}

    logger.info("Webhook raw body: %s", json.dumps(body, ensure_ascii=False)[:1000])

    result = body.get("result", body)
    event_name = result.get("event_name", body.get("event_name", ""))
    logger.info("Webhook event: %s", event_name)

    if event_name == "message.text.received":
        await handle_user_message(result)
    elif not event_name and result.get("message"):
        logger.info("Fallback: treating as text message")
        await handle_user_message(result)

    return {"message": "Success"}


def _fmt_vnd(amount: float) -> str:
    return fmt_vnd(amount)


async def handle_user_message(result: dict):
    msg = result.get("message", {})
    sender = msg.get("from", {})
    chat = msg.get("chat", {})

    user_id = sender.get("id", "")
    chat_id = chat.get("id", "")
    display_name = sender.get("display_name", "")
    text = (msg.get("text") or "").strip()

    if not chat_id or not text:
        return

    tracking_id = user_id or chat_id
    await upsert_user(tracking_id, display_name)
    cmd = text.lower().strip()

    if cmd in ("vi", "ví", "wallet", "sodu", "số dư"):
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

    if cmd in ("caidat", "cài đặt", "setting", "stk"):
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

    if cmd.startswith("caidat ") and len(cmd.split()) >= 4:
        parts = text.split(None, 3)
        bank_name = parts[1]
        bank_account = parts[2]
        bank_holder = parts[3] if len(parts) > 3 else ""
        await update_bank_info(tracking_id, bank_name, bank_account, bank_holder)
        await send_text(
            config.ZALO_BOT_TOKEN, chat_id,
            f"✅ Đã lưu thông tin ngân hàng!\n\n"
            f"🏧 Ngân hàng: {bank_name}\n"
            f"💳 Số TK: {bank_account}\n"
            f"👤 Chủ TK: {bank_holder}\n\n"
            f"Giờ khi rút tiền, bạn chỉ cần gõ:\n"
            f"rut [số tiền]\n\n"
            f"Ví dụ: rut 50000 🎉"
        )
        return

    if cmd in ("ruttien", "rút tiền", "rut tien", "withdraw"):
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

    if cmd.startswith("rut ") and len(cmd.split()) >= 2:
        parts = text.split(None, 1)
        try:
            amount = float(parts[1].replace(",", "").replace(".", ""))
        except ValueError:
            await send_text(config.ZALO_BOT_TOKEN, chat_id,
                "🤔 Mình không hiểu số tiền. Thử lại nhé!\n"
                "Ví dụ: rut 50000")
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

    if cmd in ("thongke", "thống kê", "stats", "lịch sử"):
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

    if cmd in ("help", "menu", "huong dan", "hướng dẫn", "start", "hi", "xin chao"):
        await send_text(
            config.ZALO_BOT_TOKEN, chat_id,
            f"Chào {display_name or 'bạn'}! 👋 Mình là Bot Hoàn Tiền Mua Sắm!\n\n"
            f"🛒 Cách nhận hoàn tiền:\n"
            f"1️⃣ Gửi link Shopee vào đây\n"
            f"2️⃣ Mình trả link hoàn tiền → mua qua link đó\n"
            f"3️⃣ Đơn giao thành công + không hoàn trả\n"
            f"4️⃣ Shopee xác nhận hoa hồng (~7-30 ngày)\n"
            f"5️⃣ Tiền tự động vào ví → rút về ngân hàng!\n\n"
            f"💰 Hoàn tiền ~1.5% giá trị đơn hàng\n\n"
            f"📝 Các lệnh:\n"
            f"• Gửi link Shopee → nhận link hoàn tiền\n"
            f"• \"vi\" → xem ví + số dư 👛\n"
            f"• \"caidat\" → lưu STK ngân hàng 🏦\n"
            f"• \"ruttien\" → rút tiền 💸\n"
            f"• \"thongke\" → thống kê 📊\n"
            f"• \"help\" → hướng dẫn này"
        )
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
                    "Gửi link sản phẩm Shopee cho mình để nhận hoàn tiền nhé! 🛒\n"
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
                f"Gửi link shopee.vn hoặc shp.ee để nhận hoàn tiền 🛒\n"
                f"Gõ \"help\" để xem hướng dẫn 😊"
            )
        return

    results: list[str] = []
    for url in urls:
        full_url = url
        if is_short_url(url):
            full_url = await resolve_redirect(url)

        aff_link = build_affiliate_link(
            product_url=full_url,
            affiliate_id=config.AFFILIATE_ID,
            sub_ids=[tracking_id],
        )

        await log_link(tracking_id, full_url, aff_link)
        results.append(aff_link)

    if len(results) == 1:
        reply = (
            f"🎉 Link hoàn tiền của bạn:\n\n"
            f"🔗 {results[0]}\n\n"
            f"💲 Hoàn ~1.5% giá trị đơn\n"
            f"📋 VD: đơn 200.000đ → hoàn ~3.000đ\n"
            f"📌 Mua qua link → đơn thành công → tiền tự vào ví!"
        )
    else:
        reply = f"🎉 {len(results)} link hoàn tiền:\n\n"
        for i, lk in enumerate(results, 1):
            reply += f"{i}. {lk}\n"
        reply += f"\n💰 Hoàn ~1.5%/đơn · Đơn thành công → tiền vào ví!"

    await send_text(config.ZALO_BOT_TOKEN, chat_id, reply)


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
        "host": config.HOST,
        "port": config.PORT,
    }


ENV_KEYS = {"AFFILIATE_ID", "ZALO_BOT_TOKEN", "ZALO_WEBHOOK_SECRET", "GEMINI_API_KEY"}


@app.post(_API_ADM + "/config")
async def api_admin_config_update(request: Request):
    body = await request.json()
    updated = []
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
async def api_admin_withdrawals(status: str = ""):
    return await admin_withdrawals_list(status)


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
                f"💰 Số tiền: {_fmt_vnd(amount)} đã hoàn về ví.\n"
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
async def api_admin_notifications():
    return await notifications_list()


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
