"""
Gemini Flash — fallback AI cho bot, đóng vai sale lôi kéo user gửi link Shopee.
"""

import logging
import httpx

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"

SYSTEM_PROMPT = (
    "Bạn là trợ lý bán hàng vui vẻ của Bot Nhận Tiền Mua Sắm. "
    "Bot giúp user nhận hoàn tiền ~1.5% khi mua hàng Shopee qua link affiliate. "
    "Nhiệm vụ: trả lời ngắn gọn (2-3 câu), thân thiện, dùng emoji, "
    "và LUÔN khéo léo hướng user gửi link sản phẩm Shopee vào chat để nhận hoàn tiền. "
    "Gợi ý: 'Bạn đang muốn mua gì trên Shopee không? Gửi link vào đây để nhận hoàn tiền nha!' "
    "Nếu user hỏi về chức năng bot: giới thiệu ngắn gọn các lệnh (vi, caidat, ruttien, thongke, help). "
    "KHÔNG bao giờ trả lời quá 4 câu. Ngôn ngữ: tiếng Việt."
)


async def ask_gemini(api_key: str, user_text: str, user_name: str = "") -> str | None:
    if not api_key:
        return None
    try:
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [
                {"role": "user", "parts": [{"text": f"[User: {user_name or 'bạn'}] {user_text}"}]}
            ],
            "generationConfig": {"maxOutputTokens": 150, "temperature": 0.8},
        }
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                f"{GEMINI_URL}?key={api_key}",
                json=payload,
            )
            if resp.status_code != 200:
                logger.warning("Gemini API error %d: %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return text.strip()
    except Exception as e:
        logger.warning("Gemini failed: %s", e)
        return None


async def test_gemini_key(api_key: str) -> dict:
    if not api_key:
        return {"ok": False, "error": "API key trống"}
    try:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "Xin chào, test kết nối."}]}],
            "generationConfig": {"maxOutputTokens": 20},
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{GEMINI_URL}?key={api_key}", json=payload)
            if resp.status_code == 200:
                return {"ok": True, "message": "Gemini Flash hoạt động tốt!"}
            else:
                return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:100]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
