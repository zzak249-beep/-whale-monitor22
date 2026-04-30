# -*- coding: utf-8 -*-
"""notifier.py -- Telegram notifications (fire-and-forget)."""
from __future__ import annotations
import aiohttp
from loguru import logger


async def notify(text: str) -> None:
    from core.config import cfg
    if not cfg.telegram_token or not cfg.telegram_chat_id:
        return
    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    payload = {
        "chat_id":    cfg.telegram_chat_id,
        "text":       text,
        "parse_mode": "Markdown",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.warning(f"Telegram error {r.status}: {body[:100]}")
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
