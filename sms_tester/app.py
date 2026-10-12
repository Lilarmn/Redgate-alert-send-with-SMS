"""
SMS Server Tester & Diagnostic Web Application
==============================================
مینی‌اپلیکیشن مستقل جهت تست و ارزیابی اتصال به سامانه پیامک (بر اساس مستندات رسمی v1.0)
کاملاً ایزوله از سرویس‌های دیگر.
"""

import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template, request

# Reconfigure stdout to UTF-8 for Windows console
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from sms_client import (
    CODE_EXPLANATIONS,
    CONFIG_PATH,
    SmsClient,
    load_config,
    save_config,
    validate_iranian_phone,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

app = Flask(__name__, template_folder=TEMPLATES_DIR)
client = SmsClient()


def load_history() -> List[Dict[str, Any]]:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_history(records: List[Dict[str, Any]]) -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(records[:100], f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[ERROR] Failed to save history: {e}")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = load_config()
    # Mask API key if requested for security, but allow view
    return jsonify({
        "status": "success",
        "config": cfg,
        "spec_reference": {
            "title": "سند راهنمای استفاده از API ارسال پیامک - نسخه ۱.۰",
            "official_base_url": "https://api.sms-gateway.example.com",
            "official_endpoint": "/api/v1/sms/send",
            "method": "POST",
            "headers": [
                {"name": "Content-Type", "value": "application/json", "required": True},
                {"name": "Accept-Language", "value": "fa یا en", "required": False},
                {"name": "Authorization", "value": "Bearer {API_KEY}", "required": True}
            ],
            "body_fields": [
                {"field": "destination", "type": "string", "example": "09121234567", "required": True},
                {"field": "message", "type": "string", "max_length": 480, "required": True}
            ]
        }
    })


@app.route("/api/config", methods=["POST"])
def update_config():
    data = request.get_json(force=True, silent=True) or {}
    current = load_config()

    sms_srv = data.get("sms_server", {})
    if "base_url" in sms_srv:
        current["sms_server"]["base_url"] = sms_srv["base_url"].strip()
    if "send_endpoint" in sms_srv:
        current["sms_server"]["send_endpoint"] = sms_srv["send_endpoint"].strip()
    if "api_key" in sms_srv:
        current["sms_server"]["api_key"] = sms_srv["api_key"].strip()
    if "accept_language" in sms_srv:
        current["sms_server"]["accept_language"] = sms_srv["accept_language"].strip()
    if "timeout_seconds" in sms_srv:
        try:
            current["sms_server"]["timeout_seconds"] = max(1.0, float(sms_srv["timeout_seconds"]))
        except (ValueError, TypeError):
            pass
    if "verify_ssl" in sms_srv:
        current["sms_server"]["verify_ssl"] = bool(sms_srv["verify_ssl"])
    if "mock_mode" in sms_srv:
        current["sms_server"]["mock_mode"] = bool(sms_srv["mock_mode"])
    if "mock_scenario" in sms_srv:
        current["sms_server"]["mock_scenario"] = sms_srv["mock_scenario"].strip()

    defaults = data.get("defaults", {})
    if "phone" in defaults:
        current["defaults"]["phone"] = defaults["phone"].strip()
    if "message" in defaults:
        current["defaults"]["message"] = defaults["message"].strip()

    if save_config(current):
        client.reload()
        return jsonify({"status": "success", "message": "تنظیمات با موفقیت ذخیره شد.", "config": current})
    return jsonify({"status": "error", "message": "خطا در ذخیره‌سازی فایل تنظیمات."}), 500


@app.route("/api/config/reset", methods=["POST"])
def reset_config():
    default_config = {
        "sms_server": {
            "base_url": "https://api.sms-gateway.example.com",
            "send_endpoint": "/api/v1/sms/send",
            "api_key": "YOUR_SMS_API_KEY_HERE",
            "accept_language": "fa",
            "timeout_seconds": 10,
            "verify_ssl": True,
            "mock_mode": False,
            "mock_scenario": "SENT_OK"
        },
        "defaults": {
            "phone": "09121234567",
            "message": "این یک پیامک آزمایشی جهت بررسی اتصال به سرویس پیامک می‌باشد."
        },
        "app": {
            "host": "127.0.0.1",
            "port": 8080,
            "debug": False
        }
    }
    if save_config(default_config):
        client.reload()
        return jsonify({"status": "success", "message": "تنظیمات به مقادیر پیش‌فرض مستندات بازگردانده شد.", "config": default_config})
    return jsonify({"status": "error", "message": "خطا در بازنشانی تنظیمات."}), 500


@app.route("/api/diagnose", methods=["POST", "GET"])
def run_diagnostics():
    """Runs deep 4-step diagnostic suite and returns latency, status and hints."""
    result = client.run_full_diagnostics()
    return jsonify(result)


@app.route("/api/send", methods=["POST"])
def send_test_sms():
    data = request.get_json(force=True, silent=True) or {}
    phone = data.get("destination", "").strip()
    message = data.get("message", "").strip()

    if not phone:
        cfg = load_config()
        phone = cfg.get("defaults", {}).get("phone", "09121234567")

    if not message:
        cfg = load_config()
        message = cfg.get("defaults", {}).get("message", "تست اتصال سامانه پیامک")

    # Send SMS using client
    result = client.send_sms(phone, message)

    # Save to history
    history = load_history()
    history_entry = {
        "id": f"test_{int(datetime.now().timestamp() * 1000)}",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "destination": phone,
        "message": message,
        "success": result.get("success", False),
        "code": result.get("code", "UNKNOWN"),
        "status_code": result.get("status_code"),
        "elapsed_ms": result.get("elapsed_ms"),
        "mode": result.get("mode", "real"),
        "explanation_fa": result.get("explanation_fa", ""),
        "full_result": result
    }
    history.insert(0, history_entry)
    save_history(history)

    return jsonify(result)


@app.route("/api/history", methods=["GET"])
def get_history():
    history = load_history()
    return jsonify({"status": "success", "history": history, "count": len(history)})


@app.route("/api/history/clear", methods=["POST"])
def clear_history():
    save_history([])
    return jsonify({"status": "success", "message": "تاریخچه تست‌ها پاکسازی شد."})


@app.route("/api/mock/toggle", methods=["POST"])
def toggle_mock():
    cfg = load_config()
    current_mock = cfg.get("sms_server", {}).get("mock_mode", False)
    cfg["sms_server"]["mock_mode"] = not current_mock
    save_config(cfg)
    client.reload()
    return jsonify({
        "status": "success",
        "mock_mode": cfg["sms_server"]["mock_mode"],
        "message": "حالت شبیه‌ساز فعال شد." if cfg["sms_server"]["mock_mode"] else "حالت سرور واقعی فعال شد."
    })


@app.route("/api/codes", methods=["GET"])
def get_code_reference():
    return jsonify({"codes": CODE_EXPLANATIONS})


def main():
    cfg = load_config()
    port = int(cfg.get("app", {}).get("port", 8080))
    host = cfg.get("app", {}).get("host", "127.0.0.1")
    debug = bool(cfg.get("app", {}).get("debug", False))

    print("=" * 65)
    print(" 🚀 SMS Server Diagnostic & Tester Mini-App")
    print(f" 🌐 Dashboard URL: http://{host}:{port}")
    print(f" 🎯 SMS Base URL:  {cfg.get('sms_server', {}).get('base_url')}")
    print(f" 📬 Endpoint:      {cfg.get('sms_server', {}).get('send_endpoint')}")
    print(f" 🧪 Mock Mode:     {'فعال (Simulation)' if cfg.get('sms_server', {}).get('mock_mode') else 'غیرفعال (Real SMS Server)'}")
    print("=" * 65)

    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
