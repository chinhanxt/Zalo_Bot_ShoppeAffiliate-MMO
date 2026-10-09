"""
Turso cloud DB — lưu user Zalo + lịch sử link đã chuyển.
Thay thế aiosqlite, dùng Turso HTTP API qua module turso.py.
"""

import turso

SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS users (
        zalo_id TEXT PRIMARY KEY, display_name TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        total_clicks INTEGER DEFAULT 0, balance REAL DEFAULT 0,
        total_earned REAL DEFAULT 0, total_withdrawn REAL DEFAULT 0,
        bank_name TEXT DEFAULT '', bank_account TEXT DEFAULT '', bank_holder TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS links (
        id INTEGER PRIMARY KEY AUTOINCREMENT, zalo_id TEXT NOT NULL,
        original_url TEXT NOT NULL, affiliate_url TEXT NOT NULL,
        commission_user REAL DEFAULT 0, status TEXT DEFAULT 'pending',
        conversion_id TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (zalo_id) REFERENCES users(zalo_id)
    )""",
    """CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, zalo_id TEXT NOT NULL,
        amount REAL NOT NULL, bank_info TEXT NOT NULL,
        status TEXT DEFAULT 'pending', note TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')), processed_at TEXT,
        FOREIGN KEY (zalo_id) REFERENCES users(zalo_id)
    )""",
    """CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, zalo_id TEXT DEFAULT '',
        type TEXT DEFAULT 'custom', message TEXT NOT NULL,
        sent_count INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS scheduled_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL,
        interval_hours INTEGER DEFAULT 24, is_active INTEGER DEFAULT 1,
        last_sent_at TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS kv_store (
        key TEXT PRIMARY KEY, value TEXT DEFAULT '',
        updated_at TEXT DEFAULT (datetime('now'))
    )""",
]


async def init_db():
    stmts = [(sql, None) for sql in SCHEMA_STATEMENTS]
    await turso.execute_batch(stmts)


# ──────────────────────────────────────────────
# KV Store (config, cookies, state)
# ──────────────────────────────────────────────

async def kv_get(key: str) -> str | None:
    r = await turso.execute("SELECT value FROM kv_store WHERE key=?", [key])
    if r["rows"]:
        return r["rows"][0]["value"]
    return None


async def kv_set(key: str, value: str):
    await turso.execute(
        "INSERT INTO kv_store (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        [key, value],
    )


# ──────────────────────────────────────────────
# Users
# ──────────────────────────────────────────────

async def upsert_user(zalo_id: str, display_name: str = ""):
    await turso.execute(
        "INSERT INTO users (zalo_id, display_name) VALUES (?, ?) "
        "ON CONFLICT(zalo_id) DO UPDATE SET display_name=excluded.display_name",
        [zalo_id, display_name],
    )


async def log_link(zalo_id: str, original_url: str, affiliate_url: str):
    await turso.execute_batch([
        ("INSERT INTO links (zalo_id, original_url, affiliate_url) VALUES (?, ?, ?)",
         [zalo_id, original_url, affiliate_url]),
        ("UPDATE users SET total_clicks = total_clicks + 1 WHERE zalo_id = ?",
         [zalo_id]),
    ])


async def get_user_stats(zalo_id: str) -> dict:
    results = await turso.execute_batch([
        ("SELECT total_clicks FROM users WHERE zalo_id = ?", [zalo_id]),
        ("SELECT original_url, affiliate_url, created_at FROM links "
         "WHERE zalo_id = ? ORDER BY created_at DESC LIMIT 5", [zalo_id]),
    ])
    total = results[0]["rows"][0]["total_clicks"] if results[0]["rows"] else 0
    return {"total_clicks": total, "recent_links": results[1]["rows"]}


# ──────────────────────────────────────────────
# Admin queries
# ──────────────────────────────────────────────

async def admin_dashboard_stats() -> dict:
    results = await turso.execute_batch([
        ("SELECT COUNT(*) c FROM users", None),
        ("SELECT COUNT(*) c FROM links", None),
        ("SELECT COUNT(*) c FROM links WHERE date(created_at)=date('now')", None),
        ("SELECT COUNT(*) c FROM links WHERE created_at >= datetime('now','-7 days')", None),
        ("SELECT COUNT(*) c FROM users WHERE created_at >= datetime('now','-7 days')", None),
        ("SELECT COUNT(DISTINCT zalo_id) c FROM links WHERE created_at >= datetime('now','-7 days')", None),
    ])
    return {
        "total_users": results[0]["rows"][0]["c"],
        "total_links": results[1]["rows"][0]["c"],
        "links_today": results[2]["rows"][0]["c"],
        "links_7d": results[3]["rows"][0]["c"],
        "users_new_7d": results[4]["rows"][0]["c"],
        "users_active_7d": results[5]["rows"][0]["c"],
    }


async def admin_daily_stats(days: int = 7) -> list[dict]:
    r = await turso.execute(
        "SELECT date(created_at) as day, COUNT(*) as count "
        "FROM links WHERE created_at >= datetime('now', ?) "
        "GROUP BY date(created_at) ORDER BY day",
        [f"-{days} days"],
    )
    return r["rows"]


async def admin_overview_batch() -> dict:
    results = await turso.execute_batch([
        ("SELECT COUNT(*) c FROM users", None),
        ("SELECT COUNT(*) c FROM links", None),
        ("SELECT COUNT(*) c FROM links WHERE date(created_at)=date('now')", None),
        ("SELECT COUNT(*) c FROM links WHERE created_at >= datetime('now','-7 days')", None),
        ("SELECT COUNT(*) c FROM users WHERE created_at >= datetime('now','-7 days')", None),
        ("SELECT COUNT(DISTINCT zalo_id) c FROM links WHERE created_at >= datetime('now','-7 days')", None),
        ("SELECT date(created_at) as day, COUNT(*) as count FROM links "
         "WHERE created_at >= datetime('now','-7 days') GROUP BY date(created_at) ORDER BY day", None),
        ("SELECT l.original_url, l.created_at, u.display_name, u.zalo_id "
         "FROM links l LEFT JOIN users u ON l.zalo_id=u.zalo_id "
         "ORDER BY l.created_at DESC LIMIT 5", None),
    ])
    return {
        "stats": {
            "total_users": results[0]["rows"][0]["c"],
            "total_links": results[1]["rows"][0]["c"],
            "links_today": results[2]["rows"][0]["c"],
            "links_7d": results[3]["rows"][0]["c"],
            "users_new_7d": results[4]["rows"][0]["c"],
            "users_active_7d": results[5]["rows"][0]["c"],
        },
        "daily": results[6]["rows"],
        "recent": results[7]["rows"],
    }


async def admin_users_list(
    page: int = 1, per_page: int = 20, search: str = "", sort: str = "links_desc"
) -> dict:
    where = ""
    params: list = []
    if search:
        where = "WHERE u.zalo_id LIKE ? OR u.display_name LIKE ?"
        params = [f"%{search}%", f"%{search}%"]

    order_map = {
        "links_desc": "u.total_clicks DESC",
        "links_asc": "u.total_clicks ASC",
        "newest": "u.created_at DESC",
        "oldest": "u.created_at ASC",
    }
    order = order_map.get(sort, "u.total_clicks DESC")

    results = await turso.execute_batch([
        (f"SELECT COUNT(*) c FROM users u {where}", params),
        (f"SELECT u.zalo_id, u.display_name, u.total_clicks, u.created_at, u.balance "
         f"FROM users u {where} ORDER BY {order} LIMIT ? OFFSET ?",
         params + [per_page, (page - 1) * per_page]),
    ])
    total = results[0]["rows"][0]["c"]
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
        "items": results[1]["rows"],
    }


async def admin_user_detail(zalo_id: str) -> dict | None:
    results = await turso.execute_batch([
        ("SELECT zalo_id, display_name, total_clicks, created_at, "
         "balance, total_earned, total_withdrawn, bank_name, bank_account, bank_holder "
         "FROM users WHERE zalo_id=?", [zalo_id]),
        ("SELECT id, original_url, affiliate_url, created_at FROM links "
         "WHERE zalo_id=? ORDER BY created_at DESC LIMIT 50", [zalo_id]),
        ("SELECT date(created_at) as day, COUNT(*) as count FROM links "
         "WHERE zalo_id=? GROUP BY date(created_at) ORDER BY day DESC LIMIT 14", [zalo_id]),
    ])
    if not results[0]["rows"]:
        return None
    user = results[0]["rows"][0]
    user["links"] = results[1]["rows"]
    user["daily"] = results[2]["rows"]
    return user


async def admin_links_list(
    page: int = 1, per_page: int = 20, search: str = "", user_filter: str = ""
) -> dict:
    conditions = []
    params: list = []
    if search:
        conditions.append("l.original_url LIKE ?")
        params.append(f"%{search}%")
    if user_filter:
        conditions.append("l.zalo_id = ?")
        params.append(user_filter)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    results = await turso.execute_batch([
        (f"SELECT COUNT(*) c FROM links l {where}", params),
        (f"SELECT l.id, l.zalo_id, l.original_url, l.affiliate_url, l.created_at, "
         f"l.commission_user, l.status, l.conversion_id, "
         f"u.display_name FROM links l LEFT JOIN users u ON l.zalo_id=u.zalo_id "
         f"{where} ORDER BY l.created_at DESC LIMIT ? OFFSET ?",
         params + [per_page, (page - 1) * per_page]),
    ])
    total = results[0]["rows"][0]["c"]
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
        "items": results[1]["rows"],
    }


async def admin_recent_activity(limit: int = 5) -> list[dict]:
    r = await turso.execute(
        "SELECT l.original_url, l.created_at, u.display_name, u.zalo_id "
        "FROM links l LEFT JOIN users u ON l.zalo_id=u.zalo_id "
        "ORDER BY l.created_at DESC LIMIT ?",
        [limit],
    )
    return r["rows"]


# ──────────────────────────────────────────────
# Bank info
# ──────────────────────────────────────────────

async def update_bank_info(zalo_id: str, bank_name: str, bank_account: str, bank_holder: str):
    await turso.execute(
        "UPDATE users SET bank_name=?, bank_account=?, bank_holder=? WHERE zalo_id=?",
        [bank_name, bank_account, bank_holder, zalo_id],
    )


async def get_bank_info(zalo_id: str) -> dict:
    r = await turso.execute(
        "SELECT bank_name, bank_account, bank_holder FROM users WHERE zalo_id=?",
        [zalo_id],
    )
    if not r["rows"]:
        return {"bank_name": "", "bank_account": "", "bank_holder": ""}
    return r["rows"][0]


# ──────────────────────────────────────────────
# Wallet
# ──────────────────────────────────────────────

async def get_wallet(zalo_id: str) -> dict:
    results = await turso.execute_batch([
        ("SELECT balance, total_earned, total_withdrawn, total_clicks FROM users WHERE zalo_id=?",
         [zalo_id]),
        ("SELECT COUNT(*) c FROM links WHERE zalo_id=? AND status='pending'",
         [zalo_id]),
    ])
    if not results[0]["rows"]:
        return {"balance": 0, "total_earned": 0, "total_withdrawn": 0, "total_clicks": 0, "pending_links": 0}
    w = results[0]["rows"][0]
    w["pending_links"] = results[1]["rows"][0]["c"]
    return w


# ──────────────────────────────────────────────
# Withdrawals
# ──────────────────────────────────────────────

async def create_withdrawal(zalo_id: str, amount: float, bank_info: str) -> dict:
    r = await turso.execute("SELECT balance FROM users WHERE zalo_id=?", [zalo_id])
    if not r["rows"] or r["rows"][0]["balance"] < amount:
        return {"ok": False, "error": "Số dư không đủ"}

    results = await turso.execute_batch([
        ("INSERT INTO withdrawals (zalo_id, amount, bank_info) VALUES (?, ?, ?)",
         [zalo_id, amount, bank_info]),
        ("UPDATE users SET balance = balance - ?, total_withdrawn = total_withdrawn + ? WHERE zalo_id = ?",
         [amount, amount, zalo_id]),
    ])
    return {"ok": True, "id": results[0]["last_insert_rowid"]}


async def admin_withdrawals_list(status: str = "") -> list[dict]:
    where = "WHERE w.status = ?" if status else ""
    params = [status] if status else []
    r = await turso.execute(
        f"SELECT w.*, u.display_name FROM withdrawals w "
        f"LEFT JOIN users u ON w.zalo_id=u.zalo_id "
        f"{where} ORDER BY w.created_at DESC LIMIT 100",
        params,
    )
    return r["rows"]


async def admin_process_withdrawal(wid: int, action: str, note: str = "") -> dict:
    r = await turso.execute("SELECT * FROM withdrawals WHERE id=?", [wid])
    if not r["rows"]:
        return {"ok": False, "error": "Không tìm thấy yêu cầu"}
    w = r["rows"][0]
    if w["status"] != "pending":
        return {"ok": False, "error": "Yêu cầu đã xử lý"}

    writes = []
    if action == "approve":
        writes.append((
            "UPDATE withdrawals SET status='approved', note=?, processed_at=datetime('now') WHERE id=?",
            [note, wid],
        ))
    elif action == "reject":
        writes.append((
            "UPDATE withdrawals SET status='rejected', note=?, processed_at=datetime('now') WHERE id=?",
            [note, wid],
        ))
        writes.append((
            "UPDATE users SET balance = balance + ?, total_withdrawn = total_withdrawn - ? WHERE zalo_id = ?",
            [w["amount"], w["amount"], w["zalo_id"]],
        ))
    if writes:
        await turso.execute_batch(writes)
    return {"ok": True, "withdrawal": w}


# ──────────────────────────────────────────────
# Notifications
# ──────────────────────────────────────────────

async def log_notification(zalo_id: str, msg_type: str, message: str, sent_count: int = 1):
    await turso.execute(
        "INSERT INTO notifications (zalo_id, type, message, sent_count) VALUES (?, ?, ?, ?)",
        [zalo_id, msg_type, message, sent_count],
    )


async def notifications_list(limit: int = 50) -> list[dict]:
    r = await turso.execute(
        "SELECT n.*, u.display_name FROM notifications n "
        "LEFT JOIN users u ON n.zalo_id=u.zalo_id "
        "ORDER BY n.created_at DESC LIMIT ?",
        [limit],
    )
    return r["rows"]


# ──────────────────────────────────────────────
# Scheduled messages
# ──────────────────────────────────────────────

async def scheduled_list() -> list[dict]:
    r = await turso.execute("SELECT * FROM scheduled_messages ORDER BY created_at DESC", [])
    return r["rows"]


async def scheduled_create(message: str, interval_hours: int) -> int:
    r = await turso.execute(
        "INSERT INTO scheduled_messages (message, interval_hours) VALUES (?, ?)",
        [message, interval_hours],
    )
    return r["last_insert_rowid"]


async def scheduled_toggle(sid: int, is_active: int) -> dict:
    await turso.execute(
        "UPDATE scheduled_messages SET is_active=? WHERE id=?",
        [is_active, sid],
    )
    return {"ok": True}


async def scheduled_delete(sid: int) -> dict:
    await turso.execute("DELETE FROM scheduled_messages WHERE id=?", [sid])
    return {"ok": True}


async def scheduled_due() -> list[dict]:
    r = await turso.execute(
        "SELECT * FROM scheduled_messages WHERE is_active=1 "
        "AND (last_sent_at='' OR datetime(last_sent_at, '+' || interval_hours || ' hours') <= datetime('now'))",
        [],
    )
    return r["rows"]


async def scheduled_mark_sent(sid: int):
    await turso.execute(
        "UPDATE scheduled_messages SET last_sent_at=datetime('now') WHERE id=?",
        [sid],
    )


async def all_user_chat_ids() -> list[str]:
    r = await turso.execute("SELECT zalo_id FROM users", [])
    return [row["zalo_id"] for row in r["rows"]]


# ──────────────────────────────────────────────
# Commission
# ──────────────────────────────────────────────

async def credit_commission(zalo_id: str, conv_id: str, order_id: str, user_commission: float) -> dict:
    results = await turso.execute_batch([
        ("SELECT zalo_id FROM users WHERE zalo_id=?", [zalo_id]),
        ("SELECT id FROM links WHERE conversion_id=? AND status='confirmed'", [conv_id]),
        ("SELECT id FROM links WHERE zalo_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1", [zalo_id]),
    ])

    if not results[0]["rows"]:
        return {"ok": False, "error": "User không tồn tại"}
    if results[1]["rows"]:
        return {"ok": False, "error": "Đã sync", "skipped": True}

    writes = []
    if results[2]["rows"]:
        link_id = results[2]["rows"][0]["id"]
        writes.append((
            "UPDATE links SET commission_user=?, status='confirmed', conversion_id=? WHERE id=?",
            [user_commission, conv_id, link_id],
        ))
    else:
        writes.append((
            "INSERT INTO links (zalo_id, original_url, affiliate_url, commission_user, status, conversion_id) "
            "VALUES (?, ?, ?, ?, 'confirmed', ?)",
            [zalo_id, f"shopee-order-{order_id}", f"conversion-{conv_id}", user_commission, conv_id],
        ))
    writes.append((
        "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE zalo_id = ?",
        [user_commission, user_commission, zalo_id],
    ))
    await turso.execute_batch(writes)
    return {"ok": True}


async def admin_update_commission(link_id: int, commission_user: float) -> dict:
    r = await turso.execute("SELECT * FROM links WHERE id=?", [link_id])
    if not r["rows"]:
        return {"ok": False, "error": "Link không tồn tại"}
    link = r["rows"][0]
    old_comm = link["commission_user"] or 0
    diff = commission_user - old_comm

    await turso.execute_batch([
        ("UPDATE links SET commission_user=?, status='confirmed' WHERE id=?",
         [commission_user, link_id]),
        ("UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE zalo_id = ?",
         [diff, diff, link["zalo_id"]]),
    ])
    return {"ok": True}
