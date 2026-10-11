"""Focused invariants only. These tests are NOT real-agent acceptance."""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from bridge import Bridge, Rejected


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        def transport(url, payload, headers):
            self.calls.append((url, payload, headers))
            if payload.get("type") == "verification":
                return 200, json.dumps({"challenge": payload["challenge"]}).encode()
            return 200, b"{}"
        self.env = patch.dict(os.environ, {"GROK_WEBHOOK_URL": "https://bot.example/trigger", "GROK_WEBHOOK_KEY": "test-only", "CALLBACK_HOSTS": "callback.example"})
        self.env.start()
        self.b = Bridge(self.tmp.name+"/test.sqlite", "https://bridge.example", "a"*48, "g"*48, transport)
        self.b.subscribe({"name": "message.created", "arguments": {"recipient": "dot"}, "delivery": {"mode": "webhook", "url": "https://callback.example/events", "secret": "whsec_"+"YWJj"*8}})

    def tearDown(self):
        self.b.db.close()
        self.env.stop()
        self.tmp.cleanup()

    def send(self, n, final=False):
        sender = "dot" if n % 2 else "grok"
        args = {"conversation_id": "test", "message_id": f"m{n}", "text": f"test-only-{n}", "final": final}
        if n > 1:
            args["reply_to"] = f"m{n-1}"
        return self.b.send(sender, args), args

    def test_duplicate_delivery_and_claim_and_four_round_limit(self):
        for n in range(1, 9):
            msg, args = self.send(n)
            self.assertTrue(self.b.send(msg["sender"], args)["duplicate"])
            self.assertTrue(self.b.dispatch_one())
            self.assertFalse(self.b.dispatch_one())
            self.assertTrue(self.b.claim(msg["recipient"], msg["id"])["claimed"])
            self.assertFalse(self.b.claim(msg["recipient"], msg["id"])["claimed"])
        self.assertEqual(len(self.calls), 9)  # verification + exactly 8 deliveries
        with self.assertRaisesRegex(Rejected, "Maximum 4 rounds"):
            self.send(9)

    def test_realistic_final_receipt_has_separate_completion(self):
        for n in range(1, 4):
            msg, _ = self.send(n, final=n == 3)
            self.b.dispatch_one()
            self.b.claim(msg["recipient"], msg["id"])
        self.b.finish("grok", "m3")
        history = self.b.history("test")
        self.assertEqual(history["conversation"]["state"], "completed")
        self.assertEqual(history["messages"][-1]["status"], "completed")
        with self.assertRaises(Rejected):
            self.send(4)

    def test_event_wakeup_without_payload_receives_only_once(self):
        self.send(1)
        self.b.dispatch_one()
        self.b.claim('grok', 'm1')
        self.send(2)
        self.assertFalse(self.b.receive('dot')['claimed'])
        self.b.dispatch_one()
        received = self.b.receive('dot')
        self.assertTrue(received['claimed'])
        self.assertEqual(received['id'], 'm2')
        self.assertFalse(self.b.receive('dot')['claimed'])

    def test_ambiguous_grok_timeout_is_not_retried(self):
        self.send(1)
        def timeout(*_args):
            raise TimeoutError()
        self.b.post = timeout
        self.b.dispatch_one()
        self.assertEqual(self.b.message("m1")["status"], "delivery_unknown")
        self.assertFalse(self.b.dispatch_one())

    def test_stop_expiration_and_reply_fork_prevention(self):
        self.send(1)
        with self.assertRaises(Rejected):
            self.send(2)  # never claimed
        self.b.stop("test")
        self.assertFalse(self.b.dispatch_one())
        self.assertEqual(self.b.message("m1")["status"], "stopped")
        with self.assertRaises(Rejected):
            self.b.claim("grok", "m1")

    def test_transport_receipt_is_not_agent_receipt(self):
        self.send(1)
        self.b.dispatch_one()
        msg = self.b.message("m1")
        self.assertEqual(msg["status"], "routine_accepted")
        self.assertIsNone(msg["received_at"])
        self.assertIsNone(msg["processed_at"])


if __name__ == "__main__":
    unittest.main()
