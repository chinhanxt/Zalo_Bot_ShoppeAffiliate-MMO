import os
from dotenv import load_dotenv

load_dotenv()

AFFILIATE_ID = os.getenv("AFFILIATE_ID", "")
ZALO_BOT_TOKEN = os.getenv("ZALO_BOT_TOKEN", "")
ZALO_WEBHOOK_SECRET = os.getenv("ZALO_WEBHOOK_SECRET", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
CRON_SECRET = os.getenv("CRON_SECRET", "")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8686"))
