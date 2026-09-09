"""AI provider adapters used by the store customer-service workflow.

The business service should depend on this small interface instead of knowing
the wire format of Dify or a particular OpenAI-compatible provider.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class DifyWorkflowClient:
    """Blocking Dify Workflow API client with bounded input and output parsing."""

    def __init__(self) -> None:
        self.base_url = os.getenv("DIFY_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("DIFY_API_KEY", "")
        self.user = os.getenv("DIFY_USER", "store-ai")
        self.timeout = max(3, int(os.getenv("DIFY_TIMEOUT_SECONDS", "8")))

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)

    def run(self, *, query: str, tenant_id: str, conversation_id: int, intent: str | None,
            knowledge_context: list[str], memories: list[str]) -> tuple[str | None, dict[str, Any]]:
        if not self.enabled:
            return None, {"outcome": "not_configured", "error_category": "configuration"}
        inputs = {
            "query": query,
            "tenant_id": tenant_id,
            "conversation_id": str(conversation_id),
            "intent": intent or "general_consultation",
            "knowledge_context": "\n".join(knowledge_context),
            "approved_customer_memory": "\n".join(memories),
        }
        payload = json.dumps({"inputs": inputs, "response_mode": "blocking", "user": self.user}, ensure_ascii=False).encode()
        request = urllib.request.Request(
            f"{self.base_url}/v1/workflows/run",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "StoreAI/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
                answer = self._extract_answer(body)
                if not answer:
                    return None, {"outcome": "error", "http_status": response.status, "error_category": "invalid_response"}
                return answer, {"outcome": "success", "http_status": response.status}
        except urllib.error.HTTPError as exc:
            category = "auth" if exc.code in (401, 403) else "rate_limit" if exc.code == 429 else "http"
            return None, {"outcome": "error", "http_status": exc.code, "error_category": category}
        except TimeoutError:
            return None, {"outcome": "error", "error_category": "timeout"}
        except (urllib.error.URLError, OSError) as exc:
            return None, {"outcome": "error", "error_category": "network", "error_message": str(getattr(exc, "reason", exc))}
        except (ValueError, TypeError, KeyError):
            return None, {"outcome": "error", "error_category": "invalid_response"}

    @staticmethod
    def _extract_answer(payload: Any) -> str | None:
        """Accept common Dify output shapes without trusting arbitrary fields."""
        if isinstance(payload, str) and payload.strip():
            return payload.strip()
        if isinstance(payload, dict):
            for key in ("answer", "text", "content"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in ("data", "outputs", "result"):
                if key in payload:
                    answer = DifyWorkflowClient._extract_answer(payload[key])
                    if answer:
                        return answer
        return None
