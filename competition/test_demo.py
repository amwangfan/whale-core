"""Run from repo root: python -m unittest discover -s competition -p 'test_*.py'."""
import json
import unittest
from server import PROFILES, action_for, chat, recall, parse_reply


class DemoTests(unittest.TestCase):
    def test_memory_isolation(self):
        for pid, p in PROFILES.items():
            _, hits = recall(pid, "我回家了，今天有点累，想打开台灯")
            self.assertTrue(hits)
            self.assertLessEqual({h["id"] for h in hits}, {m["id"] for m in p["memories"]})

    def test_different_light(self):
        self.assertEqual(action_for("打开台灯", "night")["value"], "warm")
        self.assertEqual(action_for("打开台灯", "maker")["value"], "white")
        self.assertIsNone(action_for("不要打开台灯", "night"))

    def test_unknown_refs_and_devices(self):
        seg, a = parse_reply(json.dumps({"segments": [{"text": "你好", "refs": ["N1", "M1"]}],
                                         "action": {"device": "shell", "value": "run"}}), [{"id": "N1"}])
        self.assertEqual(seg[0]["refs"], ["N1"])
        self.assertIsNone(a)

    def test_model_injection(self):
        def model(messages):
            self.assertNotIn("[M1]", messages[0]["content"])
            self.assertIn("[N1]", messages[0]["content"])
            return json.dumps({"segments": [{"text": "暖光适合休息", "refs": ["N1", "M1"]}]})
        result = chat({"profile": "night", "text": "回家打开台灯"}, model)
        self.assertEqual(result["segments"][0]["refs"], ["N1"])

    def test_unknown_profile(self):
        with self.assertRaises(ValueError):
            chat({"profile": "owner", "text": "hi"}, lambda _: "{}")


if __name__ == "__main__":
    unittest.main()
