import tempfile
import unittest
import base64
import hashlib
import json
from pathlib import Path

import app
from wechat_adapter import WeComAPI, WeComCrypto, WeComProtocolError, truncate_utf8


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


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

    def test_external_customer_id_reuses_customer(self):
        first = self.service.chat({"message": "你们几点营业？", "channel": "wechat_official_account", "external_customer_id": "wechat:test-openid"})
        second = self.service.chat({"message": "还有套餐吗？", "channel": "wechat_official_account", "external_customer_id": "wechat:test-openid"})
        self.assertEqual(first["customer_id"], second["customer_id"])
        self.assertEqual(len(self.db.query("SELECT * FROM customers")), 1)

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
        self.assertIsNone(result["intent"])
        self.assertTrue(result["answer"].endswith("AI回复不作为治疗依据，建议转人工评估。"))

    def test_related_knowledge_is_not_used_as_direct_answer_when_intent_is_not_covered(self):
        self.service.llm.complete = lambda *_: "可以根据当前头皮状态先由美容师评估。"
        result = self.service.chat({"message": "深层清洁能改善黑头吗？"})
        self.assertEqual(result["reply_mode"], "model_assisted")
        self.assertIsNone(result["intent"])
        self.assertIn("美容师评估", result["answer"])

    def test_precautions_intent_uses_published_precautions(self):
        result = self.service.chat({"message": "护理前有什么注意事项？"})
        self.assertEqual(result["intent"], "precautions")
        self.assertEqual(result["reply_mode"], "knowledge_direct")
        self.assertIn("红肿", result["answer"])

    def test_package_question_is_recognized_without_unnecessary_handoff(self):
        result = self.service.chat({"message": "有套餐吗？"})
        self.assertEqual(result["intent"], "packages")
        self.assertEqual(result["reply_mode"], "knowledge_direct")
        self.assertFalse(result["handoff"])
        self.assertIsNone(result["task_id"])
        self.assertIn("项目组合", result["answer"])
        self.assertNotIn("优惠、团购", result["answer"])


class WeComProtocolTests(unittest.TestCase):
    def setUp(self):
        self.aes_key = base64.b64encode(bytes(range(32))).decode("ascii").rstrip("=")
        self.crypto = WeComCrypto("callback-token", self.aes_key, "ww-corp-id")

    def test_callback_signature_and_aes_round_trip(self):
        event = b"<xml><Event><![CDATA[kf_msg_or_event]]></Event><Token><![CDATA[pull-token]]></Token><OpenKfId><![CDATA[kf-id]]></OpenKfId></xml>"
        encrypted = self.crypto.encrypt_for_test(event, b"0123456789abcdef")
        signature = self.crypto.signature("1700000000", "nonce", encrypted)
        envelope = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>".encode()
        parsed = self.crypto.decrypt_callback(envelope, signature, "1700000000", "nonce")
        self.assertEqual(parsed["Event"], "kf_msg_or_event")
        self.assertEqual(parsed["Token"], "pull-token")
        self.assertEqual(parsed["OpenKfId"], "kf-id")

    def test_invalid_signature_and_corp_id_are_rejected(self):
        encrypted = self.crypto.encrypt_for_test(b"echo", b"0123456789abcdef")
        with self.assertRaises(WeComProtocolError):
            self.crypto.decrypt_echo("invalid", "1", "2", encrypted)
        other = WeComCrypto("callback-token", self.aes_key, "different-corp")
        with self.assertRaises(WeComProtocolError):
            other.decrypt(encrypted)

    def test_access_token_is_cached_and_api_bodies_match_contract(self):
        calls = []
        responses = [
            {"errcode": 0, "access_token": "secret-access-token", "expires_in": 7200},
            {"errcode": 0, "next_cursor": "next", "has_more": 0, "msg_list": []},
            {"errcode": 0, "msgid": "sent-id"},
        ]

        def fake_urlopen(request, timeout):
            calls.append(request)
            return FakeResponse(responses.pop(0))

        api = WeComAPI("corp", "secret", base_url="https://wecom.invalid", urlopen=fake_urlopen)
        sync_result = api.sync_messages("pull-token", "kf-id")
        send_result = api.send_text("external-user", "kf-id", "您好")
        self.assertEqual(sync_result["next_cursor"], "next")
        self.assertEqual(send_result["msgid"], "sent-id")
        self.assertEqual(sum("gettoken" in request.full_url for request in calls), 1)
        sync_body = json.loads(calls[1].data)
        send_body = json.loads(calls[2].data)
        self.assertEqual(sync_body["token"], "pull-token")
        self.assertEqual(sync_body["open_kfid"], "kf-id")
        self.assertEqual(send_body["touser"], "external-user")
        self.assertEqual(send_body["text"]["content"], "您好")

    def test_utf8_reply_limit_does_not_split_characters(self):
        value = truncate_utf8("美" * 1000)
        self.assertLessEqual(len(value.encode("utf-8")), 2048)
        self.assertTrue(value.endswith("美"))


if __name__ == "__main__":
    unittest.main()
