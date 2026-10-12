"""
Unit and Integration Tests for SMS Tester Mini-App
===================================================
Tests API endpoints, diagnostic engine, configuration persistence, and mock dispatch.
"""

import json
import os
import unittest
from app import app, client
from sms_client import load_config, save_config, validate_iranian_phone


class SmsTesterTests(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    def test_phone_validation(self):
        valid, clean = validate_iranian_phone("09121234567")
        self.assertTrue(valid)
        self.assertEqual(clean, "09121234567")

        valid, clean = validate_iranian_phone("+989121234567")
        self.assertTrue(valid)
        self.assertEqual(clean, "09121234567")

        valid, clean = validate_iranian_phone("9121234567")
        self.assertTrue(valid)
        self.assertEqual(clean, "09121234567")

        valid, _ = validate_iranian_phone("1234")
        self.assertFalse(valid)

    def test_index_page(self):
        resp = self.app.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("تستر و عیب‌یاب سرویس پیامک".encode("utf-8"), resp.data)

    def test_get_config_and_spec(self):
        resp = self.app.get("/api/config")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "success")
        self.assertIn("spec_reference", data)
        self.assertEqual(data["spec_reference"]["official_base_url"], "https://api.sms-gateway.example.com")
        self.assertEqual(data["spec_reference"]["official_endpoint"], "/api/v1/sms/send")

    def test_update_config(self):
        payload = {
            "sms_server": {
                "base_url": "https://api.sms-gateway.example.com",
                "send_endpoint": "/api/v1/sms/send",
                "api_key": "TEST_KEY_123",
                "timeout_seconds": 12
            }
        }
        resp = self.app.post("/api/config", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["config"]["sms_server"]["api_key"], "TEST_KEY_123")
        self.assertEqual(data["config"]["sms_server"]["timeout_seconds"], 12)

    def test_reset_config(self):
        resp = self.app.post("/api/config/reset")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["config"]["sms_server"]["base_url"], "https://api.sms-gateway.example.com")

    def test_mock_send_all_scenarios(self):
        # 1. SENT_OK
        save_config({
            "sms_server": {
                "base_url": "https://api.sms-gateway.example.com",
                "send_endpoint": "/api/v1/sms/send",
                "api_key": "MOCK_KEY",
                "mock_mode": True,
                "mock_scenario": "SENT_OK",
                "verify_ssl": True
            }
        })
        resp = self.app.post("/api/send", json={
            "destination": "09121234567",
            "message": "تست پیامک"
        })
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["success"])
        self.assertEqual(data["code"], "SENT_OK")
        self.assertEqual(data["status_code"], 200)
        self.assertIn("messages", data["data"])

        # 2. UNAUTHORIZED
        cfg = load_config()
        cfg["sms_server"]["mock_scenario"] = "UNAUTHORIZED"
        save_config(cfg)

        resp = self.app.post("/api/send", json={
            "destination": "09121234567",
            "message": "تست پیامک"
        })
        data = json.loads(resp.data)
        self.assertFalse(data["success"])
        self.assertEqual(data["code"], "UNAUTHORIZED")
        self.assertEqual(data["status_code"], 401)

        # 3. RATE_LIMIT_EXCEEDED
        cfg["sms_server"]["mock_scenario"] = "RATE_LIMIT_EXCEEDED"
        save_config(cfg)

        resp = self.app.post("/api/send", json={
            "destination": "09121234567",
            "message": "تست پیامک"
        })
        data = json.loads(resp.data)
        self.assertFalse(data["success"])
        self.assertEqual(data["code"], "RATE_LIMIT_EXCEEDED")
        self.assertEqual(data["status_code"], 429)

    def test_history_and_clear(self):
        resp = self.app.get("/api/history")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(isinstance(data["history"], list))

        clear_resp = self.app.post("/api/history/clear")
        self.assertEqual(clear_resp.status_code, 200)

        resp_after = self.app.get("/api/history")
        data_after = json.loads(resp_after.data)
        self.assertEqual(len(data_after["history"]), 0)

    def tearDown(self):
        # Reset back to standard defaults
        self.app.post("/api/config/reset")


if __name__ == "__main__":
    unittest.main()
