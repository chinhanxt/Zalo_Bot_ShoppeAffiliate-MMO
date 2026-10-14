import os
from dotenv import load_dotenv

load_dotenv()

AFFILIATE_ID = os.getenv("AFFILIATE_ID", "")
ZALO_BOT_TOKEN = os.getenv("ZALO_BOT_TOKEN", "")
ZALO_WEBHOOK_SECRET = os.getenv("ZALO_WEBHOOK_SECRET", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
CRON_SECRET = os.getenv("CRON_SECRET", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "chinhanxt")
ADMIN_SLUG = os.getenv("ADMIN_SLUG", "uRZ5mr1A1JY")
BOT_DOMAIN = os.getenv("BOT_DOMAIN", "zalo-bot-shopee.freepro.online")
USER_SHARE_PCT = float(os.getenv("USER_SHARE_PCT", "30"))
COMMISSION_MIN_PCT = float(os.getenv("COMMISSION_MIN_PCT", "2"))
COMMISSION_MAX_PCT = float(os.getenv("COMMISSION_MAX_PCT", "4"))
COMMISSION_CAP_VND = float(os.getenv("COMMISSION_CAP_VND", "30000"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8686"))
