# Zalo Cashback Bot

Bot Zalo OA chuyển đổi link Shopee → link affiliate hoàn tiền.

## Luồng hoạt động

```
User (Zalo) gửi link Shopee
       │
       ▼
Zalo OA Webhook → FastAPI (:8686)
       │
       ├─ extract_shopee_urls() — regex bắt link shopee.vn / shp.ee
       ├─ resolve_redirect()   — nếu short URL → resolve 302
       ├─ build_affiliate_link() — ghép an_redir + affiliate_id + sub_id=zalo_user_id
       ├─ log_link() → SQLite
       │
       ▼
Trả link affiliate về khung chat Zalo
```

## Cài đặt

```bash
cd ~/ideas-cashback-bot/zalo-cashback-bot

# Tạo venv
python3 -m venv .venv && source .venv/bin/activate

# Cài deps
pip install -r requirements.txt

# Cấu hình
cp .env.example .env
# Sửa .env: điền AFFILIATE_ID, ZALO_OA_ACCESS_TOKEN, ZALO_OA_SECRET_KEY

# Chạy
python main.py
```

## Lấy AFFILIATE_ID

1. Đăng nhập https://affiliate.shopee.vn
2. Dashboard → xem ID affiliate (dãy số dài, vd: `14354840000`)
3. Điền vào `.env`

## Cấu hình Zalo OA Webhook

1. Vào https://oa.zalo.me → chọn OA → Quản lý → Cài đặt webhook
2. URL: `https://<domain-hoặc-ngrok>/webhook`
3. Đăng ký sự kiện: `user_send_text`, `follow`
4. Copy Access Token + Secret Key vào `.env`

## Test nhanh (không cần Zalo)

```bash
# Health check
curl http://localhost:8686/health

# Test chuyển link
curl "http://localhost:8686/test-link?url=https://shopee.vn/product/68475578/27971600849"
```

## Lệnh bot

| Tin nhắn | Phản hồi |
|----------|----------|
| Link Shopee | Trả link affiliate hoàn tiền |
| `thongke` | Xem lịch sử + số link đã tạo |
| `help` | Hướng dẫn sử dụng |
