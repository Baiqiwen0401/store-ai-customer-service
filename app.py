"""Single-process MVP server for the store AI customer service.

The default path is dependency-free: SQLite, stdlib HTTP server, retrieval and
guardrails are enough to run a pilot. Configure an OpenAI-compatible endpoint
with LLM_API_KEY to enable model-generated replies; all business actions still
go through the local rule-controlled tools.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).parent
DB_PATH = Path(os.getenv("STORE_AI_DB_PATH", ROOT / "runtime" / "store-ai.sqlite3"))
HOST = os.getenv("STORE_AI_HOST", "127.0.0.1")
PORT = int(os.getenv("STORE_AI_PORT", "8000"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
  tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL, business_hours TEXT,
  address TEXT, phone TEXT, welcome_message TEXT
);
CREATE TABLE IF NOT EXISTS customers (
  customer_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
  name TEXT, phone TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, phone)
);
CREATE TABLE IF NOT EXISTS consents (
  consent_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
  customer_id INTEGER NOT NULL, consent_type TEXT NOT NULL, granted_at TEXT NOT NULL,
  UNIQUE(tenant_id, customer_id, consent_type)
);
CREATE TABLE IF NOT EXISTS conversations (
  conversation_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
  customer_id INTEGER, channel TEXT NOT NULL, status TEXT NOT NULL,
  handoff_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  message_id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL, confidence REAL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge (
  knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
  title TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'published',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
  memory_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
  customer_id INTEGER NOT NULL, memory_type TEXT NOT NULL, content TEXT NOT NULL,
  source TEXT NOT NULL, confidence REAL NOT NULL, status TEXT NOT NULL DEFAULT 'candidate',
  expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
  customer_id INTEGER, conversation_id INTEGER, task_type TEXT NOT NULL,
  summary TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', assignee TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_tenant ON knowledge(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_memory_customer ON memories(tenant_id, customer_id, status);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StoreDB:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.executescript(SCHEMA)
        self.seed()

    def seed(self):
        with self.lock, self.conn:
            exists = self.conn.execute("SELECT 1 FROM stores LIMIT 1").fetchone()
            if exists:
                return
            ts = now()
            self.conn.execute("INSERT INTO stores VALUES (?,?,?,?,?,?)", (
                "demo-beauty", "悦己美容馆", "10:00-21:00", "杭州市西湖区示例路 88 号",
                "0571-88888888", "您好，我是悦己美容馆 AI 客服，很高兴为您服务。",
            ))
            docs = [
                ("项目价格", "基础补水护理 198 元，深层清洁 268 元，敏感肌舒缓护理 298 元；单次护理约 60 分钟。"),
                ("营业与预约", "营业时间为每天 10:00-21:00。AI 只能登记预约意向，门店确认后才算预约成功。"),
                ("护理注意事项", "如有红肿、破损、明显过敏、孕期或正在接受皮肤治疗，请先由美容师评估。"),
                ("门店地址", "地址：杭州市西湖区示例路 88 号。电话：0571-88888888。"),
                ("服务边界", "不得承诺根治、永久有效或百分百效果；涉及疾病、过敏、退款、投诉请转人工。"),
            ]
            self.conn.executemany(
                "INSERT INTO knowledge(tenant_id,title,content,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                [("demo-beauty", t, c, "published", ts, ts) for t, c in docs],
            )

    def query(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args=()):
        with self.lock, self.conn:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    def close(self):
        with self.lock:
            self.conn.close()


class LLMClient:
    def __init__(self):
        self.key = os.getenv("LLM_API_KEY")
        self.base = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")
        self.timeout = int(os.getenv("LLM_TIMEOUT_SECONDS", "12"))

    def complete(self, system: str, user: str) -> str | None:
        if not self.key:
            return None
        payload = json.dumps({"model": self.model, "temperature": 0.2, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}
        ]}).encode()
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 StoreAICustomerService/0.1",
                "Authorization": f"Bearer {self.key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read())
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            return None


class CustomerService:
    RISK_RE = re.compile(r"过敏|红肿|破损|疾病|孕期|怀孕|医美|注射|退款|投诉|纠纷|根治|永久|保证有效")
    APPOINT_RE = re.compile(r"预约|预定|有时间|有空|想做|安排|周[一二三四五六日天]|上午|下午|晚上")
    PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
    NAME_RE = re.compile(r"(?:我叫|我是|姓名是)([\u4e00-\u9fa5]{2,4})")
    BUDGET_RE = re.compile(r"预算[^0-9]{0,5}(\d{2,5})\s*元?")
    TIME_RE = re.compile(r"(周[一二三四五六日天](?:上午|下午|晚上)?|上午|下午|晚上)")
    DIRECT_INTENTS = {
        "price": ("多少钱", "价格", "收费", "费用", "价目"),
        "services": ("有哪些项目", "有什么项目", "门店项目", "服务项目", "哪些服务", "有什么服务", "做什么项目", "做什么护理"),
        "address": ("地址", "怎么去", "在哪里", "位置", "电话", "联系"),
        "hours": ("营业时间", "几点开", "几点关", "开门", "下班"),
        "appointment": ("预约", "预定", "有时间", "有空", "安排"),
    }
    CLINICAL_NOTICE = "\n\nAI回复不作为治疗依据，建议转人工评估。"

    def __init__(self, db: StoreDB):
        self.db = db
        self.llm = LLMClient()

    def customer(self, tenant_id: str, customer_id: int | None, name: str | None, phone: str | None, memory_consent: bool) -> int:
        ts = now()
        if customer_id:
            row = self.db.one("SELECT customer_id FROM customers WHERE customer_id=? AND tenant_id=?", (customer_id, tenant_id))
            if row:
                return customer_id
        if phone and memory_consent:
            row = self.db.one("SELECT customer_id FROM customers WHERE tenant_id=? AND phone=?", (tenant_id, phone))
            if row:
                if name:
                    self.db.execute("UPDATE customers SET name=?,updated_at=? WHERE customer_id=?", (name, ts, row["customer_id"]))
                return row["customer_id"]
        cid = self.db.execute("INSERT INTO customers(tenant_id,name,phone,created_at,updated_at) VALUES(?,?,?,?,?)", (tenant_id, name, phone if memory_consent else None, ts, ts))
        if memory_consent:
            self.db.execute("INSERT OR IGNORE INTO consents(tenant_id,customer_id,consent_type,granted_at) VALUES(?,?,?,?)", (tenant_id, cid, "long_term_memory", ts))
        return cid

    def retrieve(self, tenant_id: str, message: str) -> list[str]:
        clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", message.lower())
        tokens = set(re.findall(r"[a-z0-9]{2,}", clean))
        for size in (3, 4):
            tokens.update(clean[i:i + size] for i in range(max(0, len(clean) - size + 1)))
        rows = self.db.query("SELECT title,content FROM knowledge WHERE tenant_id=? AND status='published'", (tenant_id,))
        scored = []
        for row in rows:
            score = sum(1 for token in tokens if token in (row["title"] + row["content"]).lower())
            scored.append((score, row["content"]))
        return [x[1] for x in sorted(scored, reverse=True) if x[0] > 0][:3]

    def direct_documents(self, tenant_id: str, intent: str) -> list[str]:
        rows = self.db.query("SELECT title,content FROM knowledge WHERE tenant_id=? AND status='published'", (tenant_id,))
        if intent == "services":
            return [
                row["content"] for row in rows
                if "项目" in row["title"] or "服务项目" in row["title"]
            ][:3]
        intent_terms = {
            "price": ("价格", "价目", "收费"),
            "address": ("地址", "电话", "联系"),
            "hours": ("营业", "时间"),
            "appointment": ("预约", "营业", "时间"),
        }
        terms = intent_terms[intent]
        return [row["content"] for row in rows if any(term in row["title"] or term in row["content"] for term in terms)][:3]

    def memories(self, tenant_id: str, customer_id: int) -> list[str]:
        rows = self.db.query("SELECT content FROM memories WHERE tenant_id=? AND customer_id=? AND status='approved' ORDER BY updated_at DESC", (tenant_id, customer_id))
        return [r["content"] for r in rows[:8]]

    @classmethod
    def direct_intent(cls, message: str) -> str | None:
        for intent, phrases in cls.DIRECT_INTENTS.items():
            if any(phrase in message for phrase in phrases):
                return intent
        return None

    def extract_memories(self, tenant_id: str, customer_id: int, message: str, memory_consent: bool):
        if not memory_consent:
            return
        facts = []
        if re.search(r"敏感肌|敏感皮肤", message): facts.append("客户自述为敏感肌")
        if re.search(r"黑头", message): facts.append("客户关注黑头/毛孔问题")
        if re.search(r"补水|缺水|干燥", message): facts.append("客户关注补水或干燥问题")
        m = self.BUDGET_RE.search(message)
        if m: facts.append(f"客户预算约 {m.group(1)} 元")
        m = self.TIME_RE.search(message)
        if m: facts.append(f"客户偏好时段：{m.group(1)}")
        ts = now()
        for fact in dict.fromkeys(facts):
            exists = self.db.one("SELECT 1 FROM memories WHERE tenant_id=? AND customer_id=? AND content=? AND status IN ('candidate','approved')", (tenant_id, customer_id, fact))
            if not exists:
                self.db.execute("INSERT INTO memories(tenant_id,customer_id,memory_type,content,source,confidence,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (tenant_id, customer_id, "customer", fact, "conversation", 0.86, "candidate", ts, ts))

    def reply(self, tenant_id: str, customer_id: int, message: str) -> tuple[str, float, bool, str | None, str]:
        store = self.db.one("SELECT * FROM stores WHERE tenant_id=?", (tenant_id,))
        memories = self.memories(tenant_id, customer_id)
        risk = self.RISK_RE.search(message)
        if risk:
            return ("这个情况需要由门店工作人员进一步了解后给您建议。为了安全起见，我先为您转接人工客服，请稍候。", 0.98, True, f"触发风险词：{risk.group(0)}", "risk_handoff")
        intent = self.direct_intent(message)
        direct_docs = self.direct_documents(tenant_id, intent) if intent else []
        if direct_docs and intent in {"price", "address", "hours"}:
            return ("根据门店已发布资料：" + " ".join(direct_docs[:2]), 0.97, False, None, "knowledge_direct")
        if direct_docs and intent == "services":
            return ("目前门店已发布的项目如下：" + " ".join(direct_docs[:3]) + " 如需了解适用情况或预约时间，可以继续告诉我您的需求。", 0.97, False, None, "knowledge_direct")
        if direct_docs and intent == "appointment":
            return ("可以帮您登记预约意向。" + " ".join(direct_docs[:2]) + " 请提供期望日期/时段、称呼和手机号，门店确认后才算预约成功。", 0.94, False, None, "knowledge_direct")
        docs = self.retrieve(tenant_id, message)
        system = (
            f"你是{store['name']}的客服。优先使用以下门店资料：{' | '.join(docs) or '暂无直接匹配的门店资料'}。"
            f"客户已确认信息：{' | '.join(memories) or '暂无'}。"
            "若资料未覆盖，可提供保守的一般性美容护理信息，但不能诊断疾病、承诺疗效、给出处方或虚构门店价格、档期、政策。"
            "回答简洁、友好；有不确定、健康或安全风险时建议转人工。"
        )
        llm_reply = self.llm.complete(system, message)
        if llm_reply:
            return llm_reply.rstrip() + self.CLINICAL_NOTICE, 0.78, False, None, "model_assisted"
        if not docs:
            if self.APPOINT_RE.search(message):
                return ("可以帮您登记预约意向。请告诉我想做的项目、期望日期/时段，以及您的称呼和手机号，门店确认后会联系您。", 0.72, False, None, "appointment_fallback")
            return ("我暂时没有在门店资料中找到这个问题的准确答案，且 AI 分析服务当前不可用。我已为您转人工确认。" + self.CLINICAL_NOTICE, 0.35, True, "知识库未命中且模型不可用", "handoff_fallback")
        return (docs[0], 0.84, False, None, "knowledge_fallback")

    def chat(self, body: dict[str, Any]) -> dict[str, Any]:
        tenant_id = body.get("tenant_id", "demo-beauty")
        message = str(body.get("message", "")).strip()
        if not message:
            raise ValueError("message 不能为空")
        memory_consent = bool(body.get("memory_consent", False))
        customer_id = self.customer(tenant_id, body.get("customer_id"), body.get("name"), body.get("phone"), memory_consent)
        ts = now()
        conversation_id = body.get("conversation_id")
        if conversation_id:
            conv = self.db.one("SELECT conversation_id FROM conversations WHERE conversation_id=? AND tenant_id=?", (conversation_id, tenant_id))
            if not conv: conversation_id = None
        if not conversation_id:
            conversation_id = self.db.execute("INSERT INTO conversations(tenant_id,customer_id,channel,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", (tenant_id, customer_id, body.get("channel", "web"), "open", ts, ts))
        self.db.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)", (conversation_id, "user", message, ts))
        self.extract_memories(tenant_id, customer_id, message, memory_consent)
        answer, confidence, handoff, reason, reply_mode = self.reply(tenant_id, customer_id, message)
        self.db.execute("INSERT INTO messages(conversation_id,role,content,confidence,created_at) VALUES(?,?,?,?,?)", (conversation_id, "assistant", answer, confidence, now()))
        if handoff:
            self.db.execute("UPDATE conversations SET status='handoff',handoff_reason=?,updated_at=? WHERE conversation_id=?", (reason, now(), conversation_id))
        task_id = None
        if handoff:
            existing = self.db.one("SELECT task_id FROM tasks WHERE conversation_id=? AND task_type='handoff' AND status='pending'", (conversation_id,))
            if not existing:
                task_id = self.db.execute("INSERT INTO tasks(tenant_id,customer_id,conversation_id,task_type,summary,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (tenant_id, customer_id, conversation_id, "handoff", f"需人工接管：{reason}。客户问题：{message[:100]}", "pending", now(), now()))
        elif self.APPOINT_RE.search(message):
            existing = self.db.one("SELECT task_id FROM tasks WHERE conversation_id=? AND task_type='appointment_lead' AND status='pending'", (conversation_id,))
            if existing:
                task_id = existing["task_id"]
            else:
                task_id = self.db.execute("INSERT INTO tasks(tenant_id,customer_id,conversation_id,task_type,summary,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (tenant_id, customer_id, conversation_id, "appointment_lead", f"客户咨询预约：{message[:120]}", "pending", now(), now()))
        return {"conversation_id": conversation_id, "customer_id": customer_id, "answer": answer, "confidence": confidence, "handoff": handoff, "handoff_reason": reason, "task_id": task_id, "reply_mode": reply_mode, "memories_saved_as": "candidate"}


DB = StoreDB()
SERVICE = CustomerService(DB)


class Handler(BaseHTTPRequestHandler):
    server_version = "StoreAICustomer/0.1"

    def log_message(self, *_):
        return

    def send_json(self, payload: Any, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            data = (ROOT / "web" / "index.html").read_bytes()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        if path == "/api/store":
            row = DB.one("SELECT * FROM stores WHERE tenant_id='demo-beauty'"); self.send_json(dict(row)); return
        if path == "/api/customers":
            self.send_json([dict(r) for r in DB.query("SELECT * FROM customers WHERE tenant_id='demo-beauty' ORDER BY updated_at DESC")]); return
        if path == "/api/tasks":
            self.send_json([dict(r) for r in DB.query("SELECT * FROM tasks WHERE tenant_id='demo-beauty' ORDER BY created_at DESC")]); return
        if path == "/api/conversations":
            self.send_json([dict(r) for r in DB.query("""
                SELECT c.*,cu.name,cu.phone,
                  (SELECT content FROM messages WHERE conversation_id=c.conversation_id ORDER BY message_id DESC LIMIT 1) AS last_message
                FROM conversations c LEFT JOIN customers cu ON cu.customer_id=c.customer_id
                WHERE c.tenant_id='demo-beauty' ORDER BY c.updated_at DESC
            """)]); return
        if path == "/api/memories":
            self.send_json([dict(r) for r in DB.query("SELECT m.*,c.name,c.phone FROM memories m LEFT JOIN customers c ON c.customer_id=m.customer_id WHERE m.tenant_id='demo-beauty' ORDER BY m.updated_at DESC")]); return
        if path == "/api/knowledge":
            self.send_json([dict(r) for r in DB.query("SELECT * FROM knowledge WHERE tenant_id='demo-beauty' ORDER BY updated_at DESC")]); return
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.body()
            if path == "/api/chat": self.send_json(SERVICE.chat(body)); return
            m = re.fullmatch(r"/api/memories/(\d+)/(approve|reject)", path)
            if m:
                status = "approved" if m.group(2) == "approve" else "rejected"
                DB.execute("UPDATE memories SET status=?,updated_at=? WHERE memory_id=? AND tenant_id='demo-beauty'", (status, now(), int(m.group(1))))
                self.send_json({"ok": True, "status": status}); return
            m = re.fullmatch(r"/api/tasks/(\d+)/(complete|cancel)", path)
            if m:
                status = "completed" if m.group(2) == "complete" else "cancelled"
                DB.execute("UPDATE tasks SET status=?,updated_at=? WHERE task_id=? AND tenant_id='demo-beauty'", (status, now(), int(m.group(1))))
                self.send_json({"ok": True, "status": status}); return
            if path == "/api/knowledge":
                title, content = str(body.get("title", "")).strip(), str(body.get("content", "")).strip()
                if not title or not content: raise ValueError("title 和 content 不能为空")
                kid = DB.execute("INSERT INTO knowledge(tenant_id,title,content,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", ("demo-beauty", title, content, "published", now(), now()))
                self.send_json({"knowledge_id": kid}); return
            self.send_json({"error": "Not found"}, 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": "服务器内部错误", "detail": str(exc)}, 500)


def main():
    print(f"AI 客服 MVP running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
