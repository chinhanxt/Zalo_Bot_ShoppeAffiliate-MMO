"""
Gửi tin nhắn qua Zalo Bot Creator API.
Docs: https://bot.zaloplatforms.com/docs/apis/sendMessage/
"""

import logging
import httpx

logger = logging.getLogger(__name__)

BOT_API_BASE = "https://bot-api.zaloplatforms.com/bot"


async def send_text(bot_token: str, chat_id: str, text: str):
    """Gửi tin nhắn text tới chat_id qua Zalo Bot API."""
    url = f"{BOT_API_BASE}{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
        data = resp.json()
        if not data.get("ok"):
            logger.error("Zalo Bot send failed: %s", data)
        return data


async def set_webhook(bot_token: str, webhook_url: str, secret_token: str):
    """Đăng ký webhook URL với Zalo Bot."""
    url = f"{BOT_API_BASE}{bot_token}/setWebhook"
    payload = {
        "url": webhook_url,
        "secret_token": secret_token,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
        data = resp.json()
        logger.info("setWebhook result: %s", data)
        return data
