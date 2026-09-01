import tempfile
import unittest
from pathlib import Path

import app


class CustomerServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = app.StoreDB(Path(self.tmp.name) / "test.db")
        self.service = app.CustomerService(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_knowledge_answer_and_customer_creation(self):
        result = self.service.chat({"message": "深层清洁多少钱？"})
        self.assertIn("268", result["answer"])
        self.assertEqual(result["reply_mode"], "knowledge_direct")
        self.assertEqual(len(self.db.query("SELECT * FROM customers")), 1)

    def test_service_overview_is_answered_from_knowledge(self):
        result = self.service.chat({"message": "门店有哪些项目？"})
        self.assertEqual(result["reply_mode"], "knowledge_direct")
        self.assertFalse(result["handoff"])
        self.assertIn("深层清洁", result["answer"])
        self.assertNotIn("不得承诺", result["answer"])

    def test_memory_is_candidate_until_approval(self):
        result = self.service.chat({"message": "我是敏感肌，周六下午想做补水，预算 300 元", "memory_consent": True})
        self.assertIsNotNone(result["task_id"])
        memories = self.db.query("SELECT * FROM memories")
        self.assertGreaterEqual(len(memories), 3)
        self.assertTrue(all(row["status"] == "candidate" for row in memories))

    def test_risk_handoff(self):
        result = self.service.chat({"message": "我怀孕了，可以做医美注射吗？"})
        self.assertTrue(result["handoff"])
        self.assertIn("人工", result["answer"])

    def test_no_memory_without_consent(self):
        self.service.chat({"message": "我是敏感肌，预算 300 元"})
        self.assertEqual(len(self.db.query("SELECT * FROM memories")), 0)

    def test_appointment_task_is_not_duplicated_in_one_conversation(self):
        first = self.service.chat({"message": "周六下午可以预约吗？"})
        self.service.chat({"conversation_id": first["conversation_id"], "customer_id": first["customer_id"], "message": "想预约深层清洁"})
        tasks = self.db.query("SELECT * FROM tasks WHERE task_type='appointment_lead'")
        self.assertEqual(len(tasks), 1)

    def test_model_assisted_unknown_answer_has_safety_notice(self):
        self.service.llm.complete = lambda *_: "可以先做日常保湿和防晒。"
        result = self.service.chat({"message": "头皮护理是否适合油性发质？"})
        self.assertEqual(result["reply_mode"], "model_assisted")
        self.assertTrue(result["answer"].endswith("AI回复不作为治疗依据，建议转人工评估。"))


if __name__ == "__main__":
    unittest.main()
