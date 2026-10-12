"""Comprehensive Unit tests for routing, deduplication, extractor, comments, and adapters."""
import datetime
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from adapters.http import HttpSmsProvider
from adapters.mock import MockProvider
from config_loader import _strip_comments, validate_config
from extractor import extract_alert_details
from router import AlertRouter, format_sms_message, mask_phone


class TestConfigAndComments(unittest.TestCase):
    def test_strip_comments_windows_path_and_stars(self):
        # Test Windows trailing backslash and asterisks inside strings
        json_str = """{
            "path": "C:\\\\data\\\\", // comment after path
            "pattern": "SQL/*PROD*", /* comment inside pattern */
            "url": "https://api.sms-gateway.example.com/api" // url with slashes
        }"""
        clean = _strip_comments(json_str)
        parsed = json.loads(clean)
        self.assertEqual(parsed["path"], "C:\\data\\")
        self.assertEqual(parsed["pattern"], "SQL/*PROD*")
        self.assertEqual(parsed["url"], "https://api.sms-gateway.example.com/api")

    def test_invalid_template_placeholder_rejected_at_startup(self):
        bad_config = {
            "server": {"port": 5000, "host": "0.0.0.0"},
            "storage": {"raw_payloads_dir": "data", "database_path": "data/db.sqlite"},
            "field_mappings": {
                "alert_id": ["id"], "alert_name": ["name"], "severity": ["sev"],
                "target": ["srv"], "message": ["msg"]
            },
            "routing": {"rules": [{"recipients": [{"phone": "09121234567"}]}]},
            "templates": {"default": "Alert {cpu} on {target}"}, # {cpu} is invalid!
            "sms": {"provider": "mock"}
        }
        with self.assertRaises(ValueError) as ctx:
            validate_config(bad_config)
        self.assertIn("Invalid placeholder '{cpu}'", str(ctx.exception))


class TestExtractorNested(unittest.TestCase):
    def test_extract_nested_fields(self):
        payload = {
            "Alert": {
                "Id": 999,
                "Type": "Memory Grant Warning",
                "Severity": "High",
                "Status": "Raised",
                "Target": {
                    "Name": "SQL01"
                },
                "Description": "Memory threshold exceeded"
            }
        }
        mappings = {
            "alert_id": ["alert.id", "id"],
            "alert_name": ["alert.type", "name"],
            "severity": ["alert.severity"],
            "status": ["alert.status"],
            "target": ["target.name", "name"],
            "message": ["alert.description", "description"],
        }
        details = extract_alert_details(payload, mappings)
        self.assertEqual(details["alert_id"], "999")
        self.assertEqual(details["alert_name"], "Memory Grant Warning")
        self.assertEqual(details["severity"], "High")
        self.assertEqual(details["target"], "SQL01")
        self.assertEqual(details["message"], "Memory threshold exceeded")


class TestRoutingAndDedup(unittest.TestCase):
    def setUp(self):
        self.config = {
            "quiet_hours": {"enabled": True, "start": "23:00", "end": "07:00"},
            "deduplication_window_minutes": 5,
            "rate_limit_per_phone_per_hour": 2,
            "rules": [
                {
                    "name": "High Rule",
                    "severity": ["High"],
                    "alert_name": "*",
                    "target": "*",
                    "recipients": [{"name": "Lead", "phone": "09121111111", "bypass_quiet_hours": True}],
                }
            ],
            "default_recipients": [{"name": "Default", "phone": "09129999999"}],
        }
        self.router = AlertRouter(self.config)

    def test_phone_masking(self):
        self.assertEqual(mask_phone("09121234567"), "0912***4567")

    def test_dedup_status_awareness(self):
        alert_raised = {"alert_name": "Deadlock", "severity": "High", "target": "SQL01", "status": "Raised"}
        alert_ended = {"alert_name": "Deadlock", "severity": "High", "target": "SQL01", "status": "Ended"}

        t0 = 1000.0
        self.assertFalse(self.router.is_deduplicated(alert_raised, now_epoch=t0))
        self.router.record_dedup_success(alert_raised, now_epoch=t0)

        # Immediate re-occurrence of 'Raised' is deduplicated
        self.assertTrue(self.router.is_deduplicated(alert_raised, now_epoch=t0 + 10))

        # But 'Ended' status must NOT be deduplicated
        self.assertFalse(self.router.is_deduplicated(alert_ended, now_epoch=t0 + 15))

    def test_message_formatting_literal_line_breaks_and_splitting(self):
        details = {
            "alert_name": "Deadlock",
            "severity": "High",
            "target": "SQL01",
            "status": "Raised",
            "message": "Long message",
        }
        # Notice literal \n in template string
        templates = {
            "high": "Alert: {alert_name}\nTarget: {target}\n{message}",
            "max_length": 500,
            "split_messages": False,
        }
        parts = format_sms_message(details, templates)
        self.assertEqual(len(parts), 1)
        self.assertIn("\nTarget: SQL01\n", parts[0])  # real line break
        self.assertNotIn("\\n", parts[0])  # no literal escaped slash-n

    def test_message_splitting(self):
        details = {
            "alert_name": "Long Alert",
            "severity": "High",
            "target": "SQL01",
            "message": "A" * 60,
        }
        templates = {
            "high": "{message}",
            "max_length": 30,
            "split_messages": True,
        }
        parts = format_sms_message(details, templates)
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[0].endswith("(1/3)"))


class TestAdaptersConfigurable(unittest.TestCase):
    def test_mock_provider(self):
        mock = MockProvider({"simulate_latency_seconds": 0, "simulate_failure": False})
        res = mock.send("09121234567", "تست")
        self.assertTrue(res.success)
        self.assertEqual(res.status_code, 200)

    @patch("urllib.request.urlopen")
    def test_configurable_http_adapter_custom_payload(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"status": "SUCCESS", "id": "100"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        # Custom provider config
        provider = HttpSmsProvider({
            "url": "https://custom-gateway.internal/send",
            "method": "POST",
            "auth_type": "header",
            "api_key": "SECRET123",
            "auth_header_name": "X-API-TOKEN",
            "body_template": {"recipient": "{phone}", "text": "{message}"},
            "success_condition": {"key": "status", "expected": "SUCCESS"},
            "max_retries": 1,
        })
        res = provider.send("09121234567", "Test")
        self.assertTrue(res.success)
        self.assertIn("SECRET123", provider.headers.values())


class TestDirectPhoneRouting(unittest.TestCase):
    def test_multiple_phones_dispatch(self):
        phones = ["09121111111", "09122222222", "09123333333"]
        router = AlertRouter({}, phone_numbers=phones)
        details = {"alert_name": "Deadlock", "severity": "High", "target": "SQL01", "status": "Raised"}
        recipients, rules = router.route_alert(details)
        self.assertEqual(len(recipients), 3)
        self.assertEqual([r["phone"] for r in recipients], phones)
        self.assertIn("all_configured_phones", rules)


class TestDatabaseManager(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_audit.db")
        from db_manager import DatabaseManager
        self.dm = DatabaseManager({
            "storage": {"database_path": self.db_path},
            "database": {"enabled": False}
        }, base_dir=self.test_dir)

    def test_save_alert_and_sms_logs(self):
        self.dm.save_alert(
            details={"alert_id": "1", "alert_name": "Deadlock", "severity": "High", "target": "SRV1", "status": "Raised", "message": "Info"},
            raw_payload='{"test": 1}',
            source_ip="127.0.0.1"
        )
        recent = self.dm.get_recent_alerts(limit=10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["alert_name"], "Deadlock")

        self.dm.save_sms_log(
            alert_name="Deadlock",
            target="SRV1",
            phone="09121234567",
            message_text="Alert text",
            provider="mock",
            status="sent",
            response_code="200"
        )
        sms_logs = self.dm.get_recent_sms_logs(limit=10)
        self.assertEqual(len(sms_logs), 1)
        self.assertEqual(sms_logs[0]["phone"], "09121234567")
        self.assertEqual(sms_logs[0]["status"], "sent")


class TestVisualPanelApi(unittest.TestCase):
    def setUp(self):
        from server import app, init_service
        import tempfile
        self.test_dir = tempfile.mkdtemp()
        cfg_file = os.path.join(self.test_dir, "test_cfg.json")
        sample_cfg = {
            "server": {"host": "0.0.0.0", "port": 8000, "auth_token": ""},
            "phone_numbers": ["09121234567"],
            "database": {"enabled": False},
            "sms": {"provider": "mock", "mock": {"simulate_latency_seconds": 0, "simulate_failure": False}},
            "field_mappings": {
                "alert_id": ["id"], "alert_name": ["name"], "severity": ["sev"], "target": ["srv"], "message": ["msg"]
            },
            "routing": {"rules": []},
            "templates": {"default": "Alert: {alert_name}"},
            "storage": {"raw_payloads_dir": os.path.join(self.test_dir, "raw"), "database_path": os.path.join(self.test_dir, "test.db")},
            "logging": {"file": os.path.join(self.test_dir, "test.log")}
        }
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump(sample_cfg, f)

        init_service(cfg_file)
        self.client = app.test_client()

    def test_dashboard_html_returned(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Redgate Alert", res.data)

    def test_api_status(self):
        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "online")
        self.assertEqual(data["provider"], "mock")

    def test_api_test_sms(self):
        res = self.client.post("/api/test/sms", json={"phone": "09129999999", "message": "Manual test"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])

    def test_dispatch_mode_in_status(self):
        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("dispatch_mode", data)

    def test_api_send_manual_alert(self):
        res = self.client.post("/api/alerts/send-manual", json={
            "alert_id": 10542,
            "alert_name": "Deadlock (SQL Server)",
            "phone": "09123456789",
            "message": "Manual Alert Send Test"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
    def test_manual_mode_suppresses_automatic_blast(self):
        from server import dispatcher
        dispatcher.dispatch_mode = "manual"
        res = self.client.post("/webhook/redgate", json={
            "alert": {
                "id": 9999,
                "name": "Manual Mode Test",
                "severity": "High",
                "target": "SQL_TEST",
                "status": "Raised",
                "description": "Testing suppression"
            }
        })
        self.assertEqual(res.status_code, 202)
        dispatcher.queue.join()
        alerts = dispatcher.db_manager.get_recent_alerts(limit=5)
        self.assertTrue(any(a["alert_name"] == "Manual Mode Test" for a in alerts))
        dispatcher.dispatch_mode = "auto"

    def test_quick_switch_dispatch_mode(self):
        res = self.client.post("/api/dispatch-mode", json={"dispatch_mode": "manual"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["dispatch_mode"], "manual")

        # Switch back to auto
        res2 = self.client.post("/api/dispatch-mode", json={"dispatch_mode": "auto"})
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.get_json()["dispatch_mode"], "auto")

    def test_quick_switch_provider_mode(self):
        res = self.client.post("/api/provider-mode", json={"provider": "mock"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["provider"], "mock")


if __name__ == "__main__":
    unittest.main()
