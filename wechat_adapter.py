"""WeChat Official Account webhook and WeCom group notification helpers.

The adapter deliberately keeps platform credentials out of the business service.
It supports the official account XML callback contract and an optional WeCom
group bot webhook for internal appointment notifications.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

LOGGER = logging.getLogger("store_ai.wechat")


def verify_signature(token: str, signature: str, timestamp: str, nonce: str) -> bool:
    if not token or not signature or not timestamp or not nonce:
        return False
    digest = hashlib.sha1("".join(sorted((token, timestamp, nonce))).encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, signature)


def parse_message(payload: bytes) -> dict[str, str] | None:
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError):
        return None
    values = {child.tag: (child.text or "").strip() for child in root}
    if values.get("MsgType") != "text" or not values.get("FromUserName"):
        return None
    return {
        "to_user": values.get("ToUserName", ""),
        "from_user": values["FromUserName"],
        "content": values.get("Content", ""),
        "msg_id": values.get("MsgId", ""),
    }


def text_reply(to_user: str, from_user: str, content: str) -> bytes:
    safe_content = content or "已收到您的消息。"
    safe_content = safe_content.replace("]]>", "]]]]><![CDATA[>")
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{safe_content}]]></Content>"
        "</xml>"
    ).encode("utf-8")


@dataclass
class WeComGroupNotifier:
    webhook: str = ""
    timeout: int = 3

    @classmethod
    def from_env(cls) -> "WeComGroupNotifier":
        return cls(
            webhook=os.getenv("WECHAT_INTERNAL_GROUP_WEBHOOK", "").strip(),
            timeout=max(1, int(os.getenv("WECHAT_GROUP_TIMEOUT_SECONDS", "3"))),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.webhook)

    def send_appointment(self, summary: str) -> bool:
        if not self.enabled:
            return False
        body = json.dumps(
            {"msgtype": "text", "text": {"content": f"【微信预约意向】\n{summary}"}},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.webhook,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "StoreAI/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read() or b"{}")
            ok = int(result.get("errcode", -1)) == 0
            if not ok:
                LOGGER.warning("WeCom group notification rejected: errcode=%s", result.get("errcode"))
            return ok
        except (urllib.error.URLError, OSError, ValueError) as exc:
            LOGGER.warning("WeCom group notification failed: %s", exc)
            return False
