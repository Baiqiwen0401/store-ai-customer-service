"""Local-first AI customer service for an individual beauty salon."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ai_gateway import DifyWorkflowClient

ROOT = Path(__file__).parent
ENVIRONMENT = os.getenv("STORE_AI_ENV", "development").strip().lower()
DB_PATH = Path(os.getenv("STORE_AI_DB_PATH", str(ROOT / "runtime" / "store-ai.sqlite3")))
HOST = os.getenv("STORE_AI_HOST", "127.0.0.1")
PORT = int(os.getenv("STORE_AI_PORT", "8000"))
TENANT_DEFAULT = os.getenv("STORE_AI_TENANT", "demo-beauty")
STAFF_ACCESS_KEY = os.getenv("STAFF_ACCESS_KEY", "")
MAX_BODY_BYTES = max(16 * 1024, int(os.getenv("STORE_AI_MAX_BODY_BYTES", "262144")))
_origins = os.getenv("STORE_AI_ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS = {origin.strip() for origin in _origins.split(",") if origin.strip()} or {"*"}
LOGGER = logging.getLogger("store_ai")
logging.basicConfig(level=os.getenv("STORE_AI_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")


def validate_startup_config() -> None:
    """Fail fast on unsafe production settings while keeping local setup zero-config."""
    if ENVIRONMENT == "production" and not STAFF_ACCESS_KEY:
        raise RuntimeError("生产环境必须配置 STAFF_ACCESS_KEY")
    if ENVIRONMENT == "production" and ALLOWED_ORIGINS == {"*"}:
        raise RuntimeError("生产环境必须配置 STORE_AI_ALLOWED_ORIGINS，禁止使用通配 CORS")
    if ENVIRONMENT == "production" and DB_PATH.suffix in {".db", ".sqlite", ".sqlite3"}:
        LOGGER.warning("生产环境当前仍使用 SQLite；阶段 2 完成前不应承载高并发或关键生产数据")


validate_startup_config()

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL, business_hours TEXT, address TEXT, phone TEXT, welcome_message TEXT, settings_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customers (customer_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, name TEXT, phone TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(tenant_id, phone));
CREATE TABLE IF NOT EXISTS consents (consent_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, customer_id INTEGER NOT NULL, consent_type TEXT NOT NULL, granted_at TEXT NOT NULL, revoked_at TEXT, UNIQUE(tenant_id, customer_id, consent_type));
CREATE TABLE IF NOT EXISTS conversations (conversation_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, customer_id INTEGER, channel TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', handoff_reason TEXT, assigned_to TEXT, ai_enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (message_id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, confidence REAL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge (knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general', intent TEXT, status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories (memory_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, customer_id INTEGER NOT NULL, memory_type TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL, source_message_id INTEGER, confidence REAL NOT NULL, status TEXT NOT NULL DEFAULT 'candidate', expires_at TEXT, reviewed_by TEXT, reviewed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (task_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, customer_id INTEGER, conversation_id INTEGER, task_type TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', assignee TEXT, due_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, conversation_id INTEGER, model TEXT, outcome TEXT NOT NULL, http_status INTEGER, latency_ms INTEGER, error_category TEXT, error_message TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (audit_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER, detail_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_knowledge_tenant ON knowledge(tenant_id, status, intent);
CREATE INDEX IF NOT EXISTS idx_memory_customer ON memories(tenant_id, customer_id, status);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, message_id);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON tasks(tenant_id, status, updated_at);
"""

class StoreDB:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True); self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False); self.conn.row_factory = sqlite3.Row
        with self.conn: self.conn.executescript(SCHEMA); self._migrate()
        self.seed()
    def _migrate(self):
        additions = {"stores": {"settings_json": "TEXT NOT NULL DEFAULT '{}'", "created_at": "TEXT", "updated_at": "TEXT"}, "consents": {"revoked_at": "TEXT"}, "conversations": {"assigned_to": "TEXT", "ai_enabled": "INTEGER NOT NULL DEFAULT 1"}, "messages": {"metadata_json": "TEXT NOT NULL DEFAULT '{}'"}, "knowledge": {"category": "TEXT NOT NULL DEFAULT 'general'", "intent": "TEXT", "version": "INTEGER NOT NULL DEFAULT 1", "source": "TEXT NOT NULL DEFAULT 'manual'"}, "memories": {"source_message_id": "INTEGER", "reviewed_by": "TEXT", "reviewed_at": "TEXT"}, "tasks": {"due_at": "TEXT"}}
        for table, cols in additions.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, spec in cols.items():
                if name not in have: self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
    def seed(self):
        existing = self.one("SELECT * FROM stores LIMIT 1")
        if existing:
            # Early MVP builds shipped with mojibake demo text. Repair only the
            # known demo tenant so real tenant-authored data is left untouched.
            if TENANT_DEFAULT == "demo-beauty" and ("�" in str(existing["name"]) or "缇庡" in str(existing["name"])):
                ts = now()
                self.execute("UPDATE stores SET name=?,business_hours=?,address=?,phone=?,welcome_message=?,updated_at=? WHERE tenant_id=?", ("悦己美容院", "10:00-21:00", "杭州市西湖区示例路 88 号", "0571-88888888", "您好，我是悦己美容院 AI 客服，很高兴为您服务。", ts, TENANT_DEFAULT))
                self.execute("DELETE FROM knowledge WHERE tenant_id=?", (TENANT_DEFAULT,))
            else:
                self._repair_knowledge_metadata()
                return
        ts = now(); self.execute("INSERT INTO stores(tenant_id,name,business_hours,address,phone,welcome_message,settings_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (TENANT_DEFAULT, "悦己美容院", "10:00-21:00", "杭州市西湖区示例路 88 号", "0571-88888888", "您好，我是悦己美容院 AI 客服，很高兴为您服务。", "{}", ts, ts))
        docs = [("项目价格", "基础补水护理 198 元；深层清洁 268 元；敏感肌舒缓护理 298 元。单次护理约 60 分钟。", "pricing", "price"), ("门店项目", "目前提供基础补水护理、深层清洁、敏感肌舒缓护理。具体适用情况到店前可由美容师评估。", "services", "services"), ("营业与预约", "营业时间为每天 10:00-21:00。AI 仅登记预约意向，门店确认后才算预约成功。", "booking", "appointment"), ("护理注意事项", "如有红肿、破损、明显过敏、孕期或正在接受皮肤治疗，请先由美容师评估。", "safety", "precautions"), ("门店地址", "地址：杭州市西湖区示例路 88 号。电话：0571-88888888。", "store", "address"), ("服务边界", "不承诺根治、永久有效或百分百效果；涉及疾病、过敏、医美注射、退款、投诉或纠纷请转人工。", "safety", "risk")]
        for title, content, category, intent in docs: self.execute("INSERT INTO knowledge(tenant_id,title,content,category,intent,status,version,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (TENANT_DEFAULT, title, content, category, intent, "published", 1, "seed", ts, ts))

    def _repair_knowledge_metadata(self):
        """Backfill intent/category for databases created by the first MVP."""
        mapping = [("项目价格", "pricing", "price"), ("营业与预约", "booking", "appointment"), ("护理注意事项", "safety", "precautions"), ("门店地址", "store", "address"), ("服务边界", "safety", "risk")]
        for title, category, intent in mapping:
            self.execute("UPDATE knowledge SET category=?,intent=?,source=COALESCE(NULLIF(source,'manual'),'migrated') WHERE tenant_id=? AND title LIKE ?", (category, intent, TENANT_DEFAULT, f"%{title}%"))
        if not self.one("SELECT 1 FROM knowledge WHERE tenant_id=? AND intent='services'", (TENANT_DEFAULT,)):
            ts = now(); self.execute("INSERT INTO knowledge(tenant_id,title,content,category,intent,status,version,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (TENANT_DEFAULT, "门店项目", "目前提供基础补水护理、深层清洁、敏感肌舒缓护理。具体适用情况到店前可由美容师评估。", "services", "services", "published", 1, "migrated", ts, ts))
    def query(self, sql: str, args=()):
        with self.lock: return self.conn.execute(sql, args).fetchall()
    def one(self, sql: str, args=()):
        rows = self.query(sql, args); return rows[0] if rows else None
    def execute(self, sql: str, args=()):
        with self.lock, self.conn: return self.conn.execute(sql, args).lastrowid
    def close(self):
        with self.lock: self.conn.close()

class LLMClient:
    def __init__(self, db: StoreDB | None = None):
        self.db = db; self.key = os.getenv("LLM_API_KEY"); self.base = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"); self.model = os.getenv("LLM_MODEL", "deepseek-chat"); self.timeout = max(3, int(os.getenv("LLM_TIMEOUT_SECONDS", "8"))); self.dify = DifyWorkflowClient(); self.last_status = {"configured": bool(self.key or self.dify.enabled), "provider": "dify" if self.dify.enabled else "openai_compatible", "model": self.model, "base_url": self.base, "outcome": "not_checked"}
    def complete(self, system: str, user: str, *, tenant_id=None, conversation_id=None) -> str | None:
        started = time.perf_counter()
        if not self.key: self._record(tenant_id, conversation_id, "not_configured", None, started, "configuration", "LLM_API_KEY 未配置"); return None
        payload = json.dumps({"model": self.model, "temperature": 0.2, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode(); req = urllib.request.Request(f"{self.base}/chat/completions", data=payload, headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "StoreAI/1.0", "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response: status = response.status; data = json.loads(response.read())
            text = str(data["choices"][0]["message"]["content"]).strip()
            if not text: raise ValueError("模型返回空内容")
            self._record(tenant_id, conversation_id, "success", status, started, None, None); return text
        except urllib.error.HTTPError as exc: self._record(tenant_id, conversation_id, "error", exc.code, started, "auth" if exc.code in (401, 403) else "http", f"HTTP {exc.code}")
        except TimeoutError: self._record(tenant_id, conversation_id, "error", None, started, "timeout", "模型请求超时")
        except (urllib.error.URLError, OSError) as exc: self._record(tenant_id, conversation_id, "error", None, started, "network", str(getattr(exc, "reason", exc)))
        except Exception as exc: self._record(tenant_id, conversation_id, "error", None, started, "invalid_response", str(exc))
        return None
    def complete_with_context(self, system: str, user: str, *, tenant_id=None, conversation_id=None, intent=None, knowledge_context=None, memories=None) -> str | None:
        """Prefer a configured Dify Workflow, otherwise keep the direct model fallback."""
        if self.dify.enabled:
            started = time.perf_counter()
            text, result = self.dify.run(query=user, tenant_id=str(tenant_id or TENANT_DEFAULT), conversation_id=int(conversation_id or 0), intent=intent, knowledge_context=list(knowledge_context or []), memories=list(memories or []))
            self._record(tenant_id, conversation_id, result.get("outcome", "error"), result.get("http_status"), started, result.get("error_category"), result.get("error_message"), model="dify-workflow")
            return text
        return self.complete(system, user, tenant_id=tenant_id, conversation_id=conversation_id)
    def _record(self, tenant_id, conversation_id, outcome, status, started, category, message, model=None):
        latency = int((time.perf_counter() - started) * 1000); self.last_status = {"configured": bool(self.key or self.dify.enabled), "provider": "dify" if self.dify.enabled else "openai_compatible", "model": model or self.model, "base_url": self.dify.base_url if self.dify.enabled else self.base, "outcome": outcome, "http_status": status, "latency_ms": latency, "error_category": category, "error_message": message}
        if self.db: self.db.execute("INSERT INTO model_events(tenant_id,conversation_id,model,outcome,http_status,latency_ms,error_category,error_message,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (tenant_id, conversation_id, model or self.model, outcome, status, latency, category, message, now()))

class CustomerService:
    RISK_RE = re.compile(r"过敏|红肿|破损|疾病|孕期|怀孕|医美|注射|退款|投诉|纠纷|根治|永久|保证有效|百分百|100%|副作用")
    APPOINT_RE = re.compile(r"预约|预定|有时间|有空|安排|周[一二三四五六日天]|上午|下午|晚上")
    BUDGET_RE = re.compile(r"预算[^0-9]{0,5}(\d{2,5})\s*元?"); TIME_RE = re.compile(r"(周[一二三四五六日天](?:上午|下午|晚上)?|上午|下午|晚上)")
    INTENT_RULES = {"packages": ("套餐", "套卡", "组合项目", "优惠套餐"), "price": ("多少钱", "价格", "收费", "费用", "价目", "怎么收费"), "services": ("有哪些项目", "有什么项目", "门店项目", "服务项目", "哪些服务", "有什么服务", "服务有哪些", "有什么护理", "哪些护理", "做什么项目", "做什么护理"), "address": ("地址", "怎么去", "在哪里", "位置", "电话", "联系"), "hours": ("营业时间", "营业吗", "营业", "几点开", "几点关", "开门", "下班"), "appointment": ("预约", "预定", "有时间", "有空", "安排"), "precautions": ("注意事项", "注意什么", "禁忌", "术后", "护理建议")}
    CLINICAL_NOTICE = "\n\nAI回复不作为治疗依据，建议转人工评估。"
    def __init__(self, db): self.db = db; self.llm = LLMClient(db)
    @classmethod
    def classify_intent(cls, message):
        for intent, phrases in cls.INTENT_RULES.items():
            if any(p in message for p in phrases): return intent, 0.97, "rule"
        return None, 0.0, "none"
    def customer(self, tenant_id, customer_id, name, phone, consent):
        ts = now()
        if customer_id and self.db.one("SELECT customer_id FROM customers WHERE customer_id=? AND tenant_id=?", (customer_id, tenant_id)): return customer_id
        stored_phone = phone if consent and phone else None
        if stored_phone:
            row = self.db.one("SELECT customer_id FROM customers WHERE tenant_id=? AND phone=?", (tenant_id, stored_phone))
            if row:
                if name: self.db.execute("UPDATE customers SET name=?,updated_at=? WHERE customer_id=?", (name, ts, row["customer_id"]))
                return row["customer_id"]
        cid = self.db.execute("INSERT INTO customers(tenant_id,name,phone,created_at,updated_at) VALUES(?,?,?,?,?)", (tenant_id, name, stored_phone, ts, ts))
        if consent: self.db.execute("INSERT OR IGNORE INTO consents(tenant_id,customer_id,consent_type,granted_at) VALUES(?,?,?,?)", (tenant_id, cid, "long_term_memory", ts))
        return cid
    def _knowledge(self, tenant_id, intent=None, published_only=True):
        clause = " AND status='published'" if published_only else ""
        if intent: return self.db.query(f"SELECT * FROM knowledge WHERE tenant_id=? AND (intent=? OR category=?) {clause} ORDER BY version DESC,updated_at DESC", (tenant_id, intent, intent))
        return self.db.query(f"SELECT * FROM knowledge WHERE tenant_id=? {clause} ORDER BY updated_at DESC", (tenant_id,))
    @staticmethod
    def _search_terms(message):
        normalized = re.sub(r"\s+", "", message.lower())
        words = set(re.findall(r"[a-z0-9][a-z0-9_-]*|[\u4e00-\u9fa5]", normalized))
        bigrams = {normalized[index:index + 2] for index in range(max(0, len(normalized) - 1)) if re.search(r"[\u4e00-\u9fa5]", normalized[index:index + 2])}
        return words | bigrams
    def retrieve_context(self, tenant_id, message, limit=4):
        terms = self._search_terms(message)
        scored = []
        for row in self._knowledge(tenant_id):
            title = str(row["title"]); content = str(row["content"]); haystack = (title + content).lower()
            matched = sorted(term for term in terms if term in haystack)
            if not matched:
                continue
            title_hits = sum(1 for term in matched if term in title.lower())
            score = min(1.0, (len(matched) + title_hits * 0.75) / max(4.0, len(terms) * 0.35))
            scored.append({"knowledge_id": row["knowledge_id"], "title": title, "content": content, "version": row["version"], "score": round(score, 3), "matched_terms": matched[:12]})
        return sorted(scored, key=lambda item: (item["score"], item["version"]), reverse=True)[:limit]
    def retrieve(self, tenant_id, message):
        return [item["content"] for item in self.retrieve_context(tenant_id, message)]
    def memories(self, tenant_id, customer_id): return [r["content"] for r in self.db.query("SELECT content FROM memories WHERE tenant_id=? AND customer_id=? AND status='approved' ORDER BY updated_at DESC LIMIT 8", (tenant_id, customer_id))]
    def extract_memories(self, tenant_id, customer_id, message, consent, source_message_id):
        if not consent: return
        facts = []
        if re.search(r"敏感肌|敏感皮肤", message): facts.append(("肤质", "客户自述为敏感肌"))
        if "黑头" in message: facts.append(("关注点", "客户关注黑头/毛孔问题"))
        if re.search(r"补水|缺水|干燥", message): facts.append(("关注点", "客户关注补水或干燥问题"))
        m = self.BUDGET_RE.search(message)
        if m: facts.append(("预算", f"客户预算约 {m.group(1)} 元"))
        m = self.TIME_RE.search(message)
        if m: facts.append(("时间偏好", f"客户偏好时段：{m.group(1)}"))
        for kind, fact in dict.fromkeys(facts):
            if not self.db.one("SELECT 1 FROM memories WHERE tenant_id=? AND customer_id=? AND content=? AND status IN ('candidate','approved')", (tenant_id, customer_id, fact)): self.db.execute("INSERT INTO memories(tenant_id,customer_id,memory_type,content,source,source_message_id,confidence,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (tenant_id, customer_id, kind, fact, "conversation", source_message_id, 0.86, "candidate", now(), now()))
    def reply(self, tenant_id, customer_id, conversation_id, message):
        store = self.db.one("SELECT * FROM stores WHERE tenant_id=?", (tenant_id,)); risk = self.RISK_RE.search(message)
        if risk: return ("这个情况需要由门店工作人员进一步了解后给您建议。为了安全起见，我先为您转接人工客服，请稍候。", 0.99, True, f"触发风险词：{risk.group(0)}", "risk_handoff", "risk")
        intent, confidence, _ = self.classify_intent(message); direct = list(self._knowledge(tenant_id, intent)) if intent else []
        if direct and intent in {"packages", "price", "address", "hours", "services", "precautions"}:
            prefix = {"services": "目前门店提供：", "precautions": "根据门店护理说明："}.get(intent, "根据门店已发布资料："); return (prefix + " ".join(r["content"] for r in direct[:3]), confidence, False, None, "knowledge_direct", intent)
        if intent == "hours" and store["business_hours"]:
            return (f"门店营业时间为：{store['business_hours']}。如需预约，我可以帮您登记预约意向。", confidence, False, None, "knowledge_direct", intent)
        if intent == "packages":
            return ("您想了解套餐的项目组合、价格还是有效期？目前门店资料中还没有已发布的套餐详情，我先不替您猜测。", 0.88, False, None, "package_clarification", intent)
        if direct and intent == "appointment": return ("可以帮您登记预约意向。请提供期望日期/时段、称呼和手机号，门店确认后才算预约成功。", 0.94, False, None, "knowledge_direct", intent)
        context = self.retrieve_context(tenant_id, message); docs = [item["content"] for item in context]; memories = self.memories(tenant_id, customer_id); system = f"你是{store['name']}的专业美容院在线客服。客户意图：{intent or 'general_consultation'}。\n门店资料（仅可作为事实依据）：{' | '.join(docs) or '暂无直接匹配'}\n已确认客户偏好：{' | '.join(memories) or '暂无'}\n资料没有覆盖的项目、价格、时间、政策不得编造；可以提供保守的一般护理建议。涉及健康风险时建议人工评估，回答简洁友好。"
        try:
            llm_reply = self.llm.complete_with_context(system, message, tenant_id=tenant_id, conversation_id=conversation_id, intent=intent, knowledge_context=docs, memories=memories)
        except TypeError:
            # Keep compatibility with simple test doubles and local adapters.
            llm_reply = self.llm.complete(system, message)
        if llm_reply: return (llm_reply.rstrip() + self.CLINICAL_NOTICE, max(0.55, min(0.9, context[0]["score"] if context else 0.55)), False, None, "model_assisted", intent)
        status = self.llm.last_status; return ("这个问题门店资料暂未覆盖，我已为您登记并转人工确认，稍后会有工作人员回复。", 0.35, True, f"AI分析服务不可用（{status.get('error_category') or status.get('outcome')}）且资料不足", "handoff_fallback", intent)
    def chat(self, body):
        tenant_id = str(body.get("tenant_id") or TENANT_DEFAULT); message = str(body.get("message") or "").strip()
        if not message: raise ValueError("message 不能为空")
        consent = bool(body.get("memory_consent")); cid = self.customer(tenant_id, body.get("customer_id"), body.get("name"), body.get("phone"), consent); conversation_id = body.get("conversation_id")
        if conversation_id and not self.db.one("SELECT conversation_id FROM conversations WHERE conversation_id=? AND tenant_id=?", (conversation_id, tenant_id)): conversation_id = None
        if not conversation_id: conversation_id = self.db.execute("INSERT INTO conversations(tenant_id,customer_id,channel,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", (tenant_id, cid, body.get("channel", "web"), "open", now(), now()))
        conv = self.db.one("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)); message_id = self.db.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)", (conversation_id, "user", message, now())); self.extract_memories(tenant_id, cid, message, consent, message_id)
        if conv["ai_enabled"] == 0 or conv["status"] in {"human", "closed"}:
            answer = "已收到您的消息，门店人工客服会在工作台中继续跟进。" if conv["status"] != "closed" else "本次会话已结束，如需继续咨询请重新发起会话。"; self.db.execute("INSERT INTO messages(conversation_id,role,content,confidence,metadata_json,created_at) VALUES(?,?,?,?,?,?)", (conversation_id, "assistant", answer, 1.0, json.dumps({"mode": "human_waiting"}, ensure_ascii=False), now())); return {"conversation_id": conversation_id, "customer_id": cid, "answer": answer, "confidence": 1.0, "handoff": conv["status"] != "closed", "reply_mode": "human_waiting", "intent": None, "task_id": None}
        answer, confidence, handoff, reason, mode, intent = self.reply(tenant_id, cid, conversation_id, message); self.db.execute("INSERT INTO messages(conversation_id,role,content,confidence,metadata_json,created_at) VALUES(?,?,?,?,?,?)", (conversation_id, "assistant", answer, confidence, json.dumps({"mode": mode, "intent": intent}, ensure_ascii=False), now()))
        if handoff: self.db.execute("UPDATE conversations SET status='handoff',handoff_reason=?,ai_enabled=0,updated_at=? WHERE conversation_id=?", (reason, now(), conversation_id))
        else: self.db.execute("UPDATE conversations SET updated_at=? WHERE conversation_id=?", (now(), conversation_id))
        task_type = "handoff" if handoff else ("appointment_lead" if self.APPOINT_RE.search(message) else None); task_id = None
        if task_type:
            existing = self.db.one("SELECT task_id FROM tasks WHERE conversation_id=? AND task_type=? AND status='pending'", (conversation_id, task_type))
            if existing: task_id = existing["task_id"]
            else: task_id = self.db.execute("INSERT INTO tasks(tenant_id,customer_id,conversation_id,task_type,summary,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (tenant_id, cid, conversation_id, task_type, (f"需人工接管：{reason}" if handoff else "客户咨询预约意向") + f"。客户问题：{message[:120]}", "pending", now(), now()))
        return {"conversation_id": conversation_id, "customer_id": cid, "answer": answer, "confidence": confidence, "handoff": handoff, "handoff_reason": reason, "task_id": task_id, "intent": intent, "reply_mode": mode, "model_status": self.llm.last_status}

DB = StoreDB(); SERVICE = CustomerService(DB)
def row_dict(row): return dict(row) if row else None


def audit_event(tenant_id, actor, action, entity_type, entity_id=None, detail=None):
    """Write a compact, tenant-scoped audit record for staff and system actions."""
    DB.execute(
        "INSERT INTO audit_log(tenant_id,actor,action,entity_type,entity_id,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (tenant_id, actor, action, entity_type, entity_id, json.dumps(detail or {}, ensure_ascii=False), now()),
    )

class Handler(BaseHTTPRequestHandler):
    server_version = "StoreAI/1.0"
    def log_message(self, *_): return
    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode()
        request_id = self.headers.get("X-Request-ID") or str(uuid.uuid4())
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        origin = self.headers.get("Origin")
        if "*" in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Request-ID", request_id)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length > MAX_BODY_BYTES:
            raise ValueError(f"请求体不能超过 {MAX_BODY_BYTES} 字节")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("请求 JSON 格式无效") from exc
    def tenant(self, body=None):
        query = parse_qs(urlparse(self.path).query)
        candidate = str(self.headers.get("X-Tenant-ID") or (body or {}).get("tenant_id") or query.get("tenant_id", [TENANT_DEFAULT])[0])
        if ENVIRONMENT == "production" and candidate != TENANT_DEFAULT and not self.staff_ok():
            raise PermissionError("租户上下文无效")
        return candidate
    def staff_ok(self):
        return (not STAFF_ACCESS_KEY and ENVIRONMENT != "production") or self.headers.get("X-Staff-Key") == STAFF_ACCESS_KEY
    def do_OPTIONS(self):
        self.send_response(204)
        origin = self.headers.get("Origin")
        if "*" in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Staff-Key,X-Tenant-ID,Idempotency-Key")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                data = (ROOT / "web" / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if path == "/api/health":
                database_ok = False
                try:
                    database_ok = DB.one("SELECT 1 AS ok")['ok'] == 1
                except Exception:
                    LOGGER.exception("health database check failed")
                healthy = database_ok
                self.send_json({"status": "ok" if healthy else "degraded", "environment": ENVIRONMENT, "database": "ok" if database_ok else "error", "model_configured": bool(SERVICE.llm.key)})
                return
            protected_get = (
                path == "/api/model-status"
                or path in {"/api/customers", "/api/tasks", "/api/conversations", "/api/memories", "/api/knowledge", "/api/audit"}
                or bool(re.fullmatch(r"/api/conversations/\d+", path))
            )
            if protected_get and not self.staff_ok():
                self.send_json({"error": "需要门店工作台权限"}, 401)
                return
            tenant = self.tenant()
            if path == "/api/store": self.send_json(row_dict(DB.one("SELECT * FROM stores WHERE tenant_id=?", (tenant,)))); return
            if path == "/api/model-status":
                last = DB.one("SELECT * FROM model_events WHERE tenant_id=? ORDER BY event_id DESC LIMIT 1", (tenant,)); self.send_json({**SERVICE.llm.last_status, "last_event": row_dict(last)}); return
            if path == "/api/customers": self.send_json([dict(r) for r in DB.query("SELECT * FROM customers WHERE tenant_id=? ORDER BY updated_at DESC", (tenant,))]); return
            if path == "/api/tasks": self.send_json([dict(r) for r in DB.query("SELECT * FROM tasks WHERE tenant_id=? ORDER BY updated_at DESC", (tenant,))]); return
            if path == "/api/conversations": self.send_json([dict(r) for r in DB.query("SELECT c.*,cu.name,cu.phone,(SELECT content FROM messages WHERE conversation_id=c.conversation_id ORDER BY message_id DESC LIMIT 1) AS last_message FROM conversations c LEFT JOIN customers cu ON cu.customer_id=c.customer_id WHERE c.tenant_id=? ORDER BY c.updated_at DESC", (tenant,))]); return
            match = re.fullmatch(r"/api/conversations/(\d+)", path)
            if match:
                conv = DB.one("SELECT c.*,cu.name,cu.phone FROM conversations c LEFT JOIN customers cu ON cu.customer_id=c.customer_id WHERE c.conversation_id=? AND c.tenant_id=?", (int(match.group(1)), tenant))
                if not conv: self.send_json({"error": "会话不存在"}, 404); return
                self.send_json({"conversation": dict(conv), "messages": [dict(r) for r in DB.query("SELECT * FROM messages WHERE conversation_id=? ORDER BY message_id", (conv["conversation_id"],))], "tasks": [dict(r) for r in DB.query("SELECT * FROM tasks WHERE conversation_id=? ORDER BY task_id DESC", (conv["conversation_id"],))]}); return
            if path == "/api/memories": self.send_json([dict(r) for r in DB.query("SELECT m.*,c.name,c.phone FROM memories m LEFT JOIN customers c ON c.customer_id=m.customer_id WHERE m.tenant_id=? ORDER BY m.updated_at DESC", (tenant,))]); return
            if path == "/api/knowledge": self.send_json([dict(r) for r in DB.query("SELECT * FROM knowledge WHERE tenant_id=? ORDER BY updated_at DESC,version DESC", (tenant,))]); return
            if path == "/api/audit": self.send_json([dict(r) for r in DB.query("SELECT * FROM audit_log WHERE tenant_id=? ORDER BY audit_id DESC LIMIT 100", (tenant,))]); return
            self.send_json({"error": "Not found"}, 404)
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, 403)
        except Exception as exc:
            LOGGER.exception("GET %s failed", path)
            payload = {"error": "服务内部错误"}
            if ENVIRONMENT != "production":
                payload["detail"] = str(exc)
            self.send_json(payload, 500)
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.body(); tenant = self.tenant(body)
            if path == "/api/chat": self.send_json(SERVICE.chat(body)); return
            if not self.staff_ok(): self.send_json({"error": "需要门店工作台权限"}, 401); return
            match = re.fullmatch(r"/api/memories/(\d+)/(approve|reject)", path)
            if match:
                status = "approved" if match.group(2) == "approve" else "rejected"; actor = str(body.get("actor") or "staff"); memory_id = int(match.group(1)); DB.execute("UPDATE memories SET status=?,reviewed_by=?,reviewed_at=?,updated_at=? WHERE memory_id=? AND tenant_id=?", (status, actor, now(), now(), memory_id, tenant)); audit_event(tenant, actor, f"memory_{status}", "memory", memory_id); self.send_json({"ok": True, "status": status}); return
            match = re.fullmatch(r"/api/tasks/(\d+)/(complete|cancel)", path)
            if match:
                status = "completed" if match.group(2) == "complete" else "cancelled"; actor = str(body.get("actor") or "staff"); task_id = int(match.group(1)); DB.execute("UPDATE tasks SET status=?,updated_at=? WHERE task_id=? AND tenant_id=?", (status, now(), task_id, tenant)); audit_event(tenant, actor, f"task_{status}", "task", task_id); self.send_json({"ok": True, "status": status}); return
            match = re.fullmatch(r"/api/conversations/(\d+)/(claim|reply|resume|close)", path)
            if match:
                conv_id, action = int(match.group(1)), match.group(2); conv = DB.one("SELECT * FROM conversations WHERE conversation_id=? AND tenant_id=?", (conv_id, tenant))
                if not conv: self.send_json({"error": "会话不存在"}, 404); return
                actor = str(body.get("assignee") or body.get("actor") or "staff")
                if action == "claim": DB.execute("UPDATE conversations SET status='human',ai_enabled=0,assigned_to=?,updated_at=? WHERE conversation_id=?", (actor, now(), conv_id)); audit_event(tenant, actor, "conversation_claim", "conversation", conv_id); self.send_json({"ok": True, "status": "human", "assigned_to": actor}); return
                if action == "reply":
                    content = str(body.get("message") or "").strip()
                    if not content: raise ValueError("回复内容不能为空")
                    DB.execute("INSERT INTO messages(conversation_id,role,content,confidence,metadata_json,created_at) VALUES(?,?,?,?,?,?)", (conv_id, "human", content, 1.0, json.dumps({"actor": actor}, ensure_ascii=False), now())); DB.execute("UPDATE conversations SET status='human',ai_enabled=0,assigned_to=?,updated_at=? WHERE conversation_id=?", (actor, now(), conv_id)); audit_event(tenant, actor, "conversation_reply", "conversation", conv_id); self.send_json({"ok": True, "status": "human"}); return
                if action == "resume": DB.execute("UPDATE conversations SET status='open',ai_enabled=1,updated_at=? WHERE conversation_id=?", (now(), conv_id)); audit_event(tenant, actor, "conversation_resume", "conversation", conv_id); self.send_json({"ok": True, "status": "open"}); return
                DB.execute("UPDATE conversations SET status='closed',ai_enabled=0,updated_at=? WHERE conversation_id=?", (now(), conv_id)); audit_event(tenant, actor, "conversation_close", "conversation", conv_id); self.send_json({"ok": True, "status": "closed"}); return
            if path == "/api/knowledge":
                title, content = str(body.get("title") or "").strip(), str(body.get("content") or "").strip()
                if not title or not content: raise ValueError("标题和内容不能为空")
                status = body.get("status", "draft") if body.get("status") in {"draft", "published"} else "draft"; kid = DB.execute("INSERT INTO knowledge(tenant_id,title,content,category,intent,status,version,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (tenant, title, content, body.get("category", "general"), body.get("intent"), status, 1, "manual", now(), now())); audit_event(tenant, str(body.get("actor") or "staff"), "knowledge_create", "knowledge", kid, {"status": status}); self.send_json({"knowledge_id": kid, "status": status}); return
            self.send_json({"error": "Not found"}, 404)
        except ValueError as exc: self.send_json({"error": str(exc)}, 400)
        except PermissionError as exc: self.send_json({"error": str(exc)}, 403)
        except Exception as exc:
            LOGGER.exception("POST %s failed", path)
            payload = {"error": "服务内部错误"}
            if ENVIRONMENT != "production":
                payload["detail"] = str(exc)
            self.send_json(payload, 500)
    def do_PUT(self):
        path = urlparse(self.path).path
        if not self.staff_ok(): self.send_json({"error": "需要门店工作台权限"}, 401); return
        try:
            body = self.body(); tenant = self.tenant(body); match = re.fullmatch(r"/api/knowledge/(\d+)/(publish|archive)", path)
            if match:
                status = "published" if match.group(2) == "publish" else "archived"; knowledge_id = int(match.group(1)); actor = str(body.get("actor") or "staff"); DB.execute("UPDATE knowledge SET status=?,updated_at=? WHERE knowledge_id=? AND tenant_id=?", (status, now(), knowledge_id, tenant)); audit_event(tenant, actor, f"knowledge_{status}", "knowledge", knowledge_id); self.send_json({"ok": True, "status": status}); return
            self.send_json({"error": "Not found"}, 404)
        except ValueError as exc: self.send_json({"error": str(exc)}, 400)
        except PermissionError as exc: self.send_json({"error": str(exc)}, 403)
        except Exception as exc:
            LOGGER.exception("PUT %s failed", path)
            payload = {"error": "服务内部错误"}
            if ENVIRONMENT != "production":
                payload["detail"] = str(exc)
            self.send_json(payload, 500)

def main():
    print(f"AI 客服 running at http://{HOST}:{PORT}"); ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

if __name__ == "__main__": main()
