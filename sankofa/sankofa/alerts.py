"""Telegram alerts (optional). Plain HTTPS call to the Bot API; no extra dependency."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, enabled: bool) -> None:
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(enabled and self.token and self.chat_id)

    def send(self, text: str) -> None:
        log.info("ALERT: %s", text.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat_id, "text": text[:4000]}, timeout=10)
        except Exception as exc:  # never let alerting kill the loop
            log.warning("telegram failed: %s", exc)
