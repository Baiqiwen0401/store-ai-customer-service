"""Official WeCom WeChat Customer Service protocol helpers.

Credentials stay in process environment variables. The callback crypto follows
the Enterprise WeChat AES-CBC contract; business processing lives in app.py.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

LOGGER = logging.getLogger("store_ai.wecom")
AUTH_ERROR_CODES = {40014, 42001, 42007, 42009}


class WeComProtocolError(ValueError):
    """The callback or API response did not satisfy the WeCom contract."""


class WeComAPIError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"WeCom API error {code}: {message}")
        self.code = code
        self.message = message


def _xml_values(payload: bytes) -> dict[str, str]:
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError) as exc:
        raise WeComProtocolError("企业微信回调 XML 无效") from exc
    return {child.tag: (child.text or "").strip() for child in root}


class WeComCrypto:
    """Verify and decrypt Enterprise WeChat encrypted callbacks."""

    block_size = 32

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        if not token or not receive_id:
            raise WeComProtocolError("回调 Token 和 CorpID 不能为空")
        if len(encoding_aes_key) != 43:
            raise WeComProtocolError("EncodingAESKey 必须为 43 位")
        try:
            self.aes_key = base64.b64decode(encoding_aes_key + "=", validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise WeComProtocolError("EncodingAESKey 格式无效") from exc
        if len(self.aes_key) != 32:
            raise WeComProtocolError("EncodingAESKey 解码后必须为 32 字节")
        self.token = token
        self.receive_id = receive_id

    def signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        parts = (self.token, str(timestamp), str(nonce), encrypted)
        return hashlib.sha1("".join(sorted(parts)).encode("utf-8")).hexdigest()

    def verify(self, signature: str, timestamp: str, nonce: str, encrypted: str) -> bool:
        if not signature or not timestamp or not nonce or not encrypted:
            return False
        return hmac.compare_digest(self.signature(timestamp, nonce, encrypted), signature)

    @classmethod
    def _unpad(cls, plaintext: bytes) -> bytes:
        if not plaintext:
            raise WeComProtocolError("回调解密结果为空")
        padding = plaintext[-1]
        if padding < 1 or padding > cls.block_size:
            raise WeComProtocolError("回调 AES 填充无效")
        if plaintext[-padding:] != bytes([padding]) * padding:
            raise WeComProtocolError("回调 AES 填充不一致")
        return plaintext[:-padding]

    @classmethod
    def _pad(cls, plaintext: bytes) -> bytes:
        padding = cls.block_size - len(plaintext) % cls.block_size
        return plaintext + bytes([padding]) * padding

    def decrypt(self, encrypted: str) -> bytes:
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
            decryptor = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.aes_key[:16])).decryptor()
            plaintext = self._unpad(decryptor.update(ciphertext) + decryptor.finalize())
        except WeComProtocolError:
            raise
        except Exception as exc:
            raise WeComProtocolError("回调 AES 解密失败") from exc
        if len(plaintext) < 20:
            raise WeComProtocolError("回调解密数据长度无效")
        message_length = struct.unpack("!I", plaintext[16:20])[0]
        message_end = 20 + message_length
        if message_end > len(plaintext):
            raise WeComProtocolError("回调消息长度字段无效")
        message = plaintext[20:message_end]
        try:
            receive_id = plaintext[message_end:].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WeComProtocolError("回调 CorpID 编码无效") from exc
        if not hmac.compare_digest(receive_id, self.receive_id):
            raise WeComProtocolError("回调 CorpID 不匹配")
        return message

    def encrypt_for_test(self, message: bytes, random_bytes: bytes | None = None) -> str:
        """Create a protocol-compatible ciphertext for deterministic tests."""
        random_bytes = random_bytes or os.urandom(16)
        if len(random_bytes) != 16:
            raise ValueError("random_bytes 必须为 16 字节")
        plaintext = random_bytes + struct.pack("!I", len(message)) + message + self.receive_id.encode("utf-8")
        encryptor = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.aes_key[:16])).encryptor()
        return base64.b64encode(encryptor.update(self._pad(plaintext)) + encryptor.finalize()).decode("ascii")

    def decrypt_echo(self, signature: str, timestamp: str, nonce: str, echo: str) -> bytes:
        if not self.verify(signature, timestamp, nonce, echo):
            raise WeComProtocolError("企业微信回调签名无效")
        return self.decrypt(echo)

    def decrypt_callback(self, payload: bytes, signature: str, timestamp: str, nonce: str) -> dict[str, str]:
        encrypted = _xml_values(payload).get("Encrypt", "")
        if not self.verify(signature, timestamp, nonce, encrypted):
            raise WeComProtocolError("企业微信回调签名无效")
        return _xml_values(self.decrypt(encrypted))


def truncate_utf8(value: str, max_bytes: int = 2048) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


# Kept only until the HTTP route is migrated in the next stage. Existing local
# deployments must not fail between independently deployable phase commits.
def verify_signature(token: str, signature: str, timestamp: str, nonce: str) -> bool:
    if not token or not signature or not timestamp or not nonce:
        return False
    digest = hashlib.sha1("".join(sorted((token, timestamp, nonce))).encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, signature)


def parse_message(payload: bytes) -> dict[str, str] | None:
    try:
        values = _xml_values(payload)
    except WeComProtocolError:
        return None
    if values.get("MsgType") != "text" or not values.get("FromUserName"):
        return None
    return {
        "to_user": values.get("ToUserName", ""),
        "from_user": values["FromUserName"],
        "content": values.get("Content", ""),
        "msg_id": values.get("MsgId", ""),
    }


def text_reply(to_user: str, from_user: str, content: str) -> bytes:
    safe_content = (content or "已收到您的消息。").replace("]]>", "]]]]><![CDATA[>")
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{safe_content}]]></Content>"
        "</xml>"
    ).encode("utf-8")


class WeComAPI:
    """Small official API client with token caching and one auth retry."""

    def __init__(
        self,
        corp_id: str,
        app_secret: str,
        *,
        base_url: str = "https://qyapi.weixin.qq.com",
        timeout: int = 5,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ):
        self.corp_id = corp_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = max(1, timeout)
        self.urlopen = urlopen
        self._access_token = ""
        self._access_token_expires_at = 0.0
        self._token_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "WeComAPI":
        return cls(
            os.getenv("WECOM_CORP_ID", "").strip(),
            os.getenv("WECOM_APP_SECRET", "").strip(),
            base_url=os.getenv("WECOM_API_BASE_URL", "https://qyapi.weixin.qq.com").strip(),
            timeout=int(os.getenv("WECOM_HTTP_TIMEOUT_SECONDS", "5")),
        )

    @property
    def configured(self) -> bool:
        return bool(self.corp_id and self.app_secret)

    def _json_request(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with self.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise WeComAPIError(exc.code, "HTTP request failed") from exc
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise WeComAPIError(-1, "network or response error") from exc
        if not isinstance(payload, dict):
            raise WeComAPIError(-1, "invalid JSON response")
        return payload

    def get_access_token(self, force_refresh: bool = False) -> str:
        if not self.configured:
            raise WeComAPIError(-2, "WECOM_CORP_ID/WECOM_APP_SECRET not configured")
        with self._token_lock:
            if not force_refresh and self._access_token and time.time() < self._access_token_expires_at:
                return self._access_token
            query = urllib.parse.urlencode({"corpid": self.corp_id, "corpsecret": self.app_secret})
            payload = self._json_request(urllib.request.Request(f"{self.base_url}/cgi-bin/gettoken?{query}"))
            code = int(payload.get("errcode", 0))
            if code:
                raise WeComAPIError(code, str(payload.get("errmsg", "gettoken failed")))
            token = str(payload.get("access_token", ""))
            if not token:
                raise WeComAPIError(-1, "gettoken response missing access_token")
            expires_in = max(60, int(payload.get("expires_in", 7200)))
            self._access_token = token
            self._access_token_expires_at = time.time() + max(30, expires_in - 300)
            return token

    def _post(self, path: str, body: dict[str, Any], retry_auth: bool = True) -> dict[str, Any]:
        access_token = self.get_access_token()
        query = urllib.parse.urlencode({"access_token": access_token})
        request = urllib.request.Request(
            f"{self.base_url}{path}?{query}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "StoreAI/1.0"},
        )
        payload = self._json_request(request)
        code = int(payload.get("errcode", 0))
        if code in AUTH_ERROR_CODES and retry_auth:
            self.get_access_token(force_refresh=True)
            return self._post(path, body, retry_auth=False)
        if code:
            raise WeComAPIError(code, str(payload.get("errmsg", "request failed")))
        return payload

    def sync_messages(self, callback_token: str, open_kfid: str, cursor: str = "", limit: int = 1000) -> dict[str, Any]:
        body: dict[str, Any] = {
            "token": callback_token,
            "open_kfid": open_kfid,
            "limit": min(1000, max(1, int(limit))),
            "voice_format": 0,
        }
        if cursor:
            body["cursor"] = cursor
        return self._post("/cgi-bin/kf/sync_msg", body)

    def send_text(self, external_userid: str, open_kfid: str, content: str) -> dict[str, Any]:
        body = {
            "touser": external_userid,
            "open_kfid": open_kfid,
            "msgtype": "text",
            "text": {"content": truncate_utf8(content or "已收到您的消息。")},
        }
        return self._post("/cgi-bin/kf/send_msg", body)


@dataclass
class WeComGroupNotifier:
    webhook: str = ""
    timeout: int = 3

    @classmethod
    def from_env(cls) -> "WeComGroupNotifier":
        webhook = os.getenv("WECOM_INTERNAL_GROUP_WEBHOOK", "").strip()
        if not webhook:
            webhook = os.getenv("WECHAT_INTERNAL_GROUP_WEBHOOK", "").strip()
        return cls(
            webhook=webhook,
            timeout=max(1, int(os.getenv("WECOM_GROUP_TIMEOUT_SECONDS", os.getenv("WECHAT_GROUP_TIMEOUT_SECONDS", "3")))),
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
