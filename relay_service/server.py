"""Web service, Visual Dashboard, and HTTP endpoints for Redgate Alert -> SMS Relay.

Endpoints:
- GET  /                     : Visual Web Dashboard & Settings Panel
- POST /webhook/redgate       : Webhook listener for incoming Redgate alerts
- POST /webhook               : Alias for Redgate webhook listener
- GET  /health                : Health check status and service statistics
- GET  /audit/recent          : Diagnostic query for recent alert dispatches
- GET  /api/status            : Live service statistics, uptime, and component statuses
- GET  /api/alerts            : Query recent incoming alerts
- GET  /api/sms-logs          : Query recent SMS delivery logs
- GET  /api/config            : Retrieve current configuration for visual editor
- POST /api/config            : Save updated configuration and hot-reload service
- POST /api/test/sms          : Directly test SMS sending to any phone number
- POST /api/test/db           : Test Microsoft SQL Server database connection
- POST /api/test/db/init      : Create / verify SQL Server alert and SMS tables
- GET  /api/logs              : Tail application log file
"""
import datetime
import hmac
import json
import logging
import os
import sys

from flask import Flask, jsonify, render_template, request

from config_loader import load_config, save_config
from dispatcher import AlertDispatcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATES_DIR)
log = logging.getLogger("relay.server")

dispatcher: AlertDispatcher = None
config: dict = None
CONFIG_FILE_PATH: str = "config.json"
SERVICE_START_TIME = datetime.datetime.now()


def _save_raw_dump(source_ip: str, raw_text: str, prefix: str = "rejected") -> str:
    """Always save incoming payload to disk even if unauthorized or malformed."""
    raw_dir = config.get("storage", {}).get("raw_payloads_dir", "data/raw_payloads") if config else "data/raw_payloads"
    if not os.path.isabs(raw_dir):
        raw_dir = os.path.join(BASE_DIR, raw_dir)
    os.makedirs(raw_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    fname = f"{prefix}_{ts}.txt"
    fpath = os.path.join(raw_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(f"Source-IP: {source_ip}\n\n{raw_text}")
    return fname


def _is_authenticated() -> bool:
    expected_token = config.get("server", {}).get("auth_token", "") if config else ""
    if not expected_token:
        return True

    auth_header = request.headers.get("Authorization", "")
    token_header = request.headers.get("X-Webhook-Token", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    elif token_header:
        token = token_header.strip()

    # Constant time comparison
    return hmac.compare_digest(token, expected_token)


def init_service(config_path: str = "config.json") -> None:
    global dispatcher, config, CONFIG_FILE_PATH, SERVICE_START_TIME
    if not os.path.isabs(config_path):
        config_path = os.path.join(BASE_DIR, config_path)
    CONFIG_FILE_PATH = config_path
    SERVICE_START_TIME = datetime.datetime.now()
    config = load_config(config_path)
    dispatcher = AlertDispatcher(config, base_dir=BASE_DIR)
    log.info("Relay service initialized successfully. SMS Provider: %s", config.get("sms", {}).get("provider"))


# ==============================================================================
# Visual Dashboard & API Endpoints
# ==============================================================================

@app.route("/", methods=["GET"])
def visual_dashboard():
    """Serves the visual management web panel."""
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def api_status():
    """Live statistics for the visual dashboard."""
    now = datetime.datetime.now()
    uptime_secs = int((now - SERVICE_START_TIME).total_seconds())

    alerts = dispatcher.db_manager.get_recent_alerts(limit=500)
    sms_logs = dispatcher.db_manager.get_recent_sms_logs(limit=500)
    sent_count = sum(1 for s in sms_logs if s.get("status") == "sent")
    failed_count = sum(1 for s in sms_logs if s.get("status") in ("failed", "exception"))

    return jsonify({
        "status": "online",
        "uptime_seconds": uptime_secs,
        "total_alerts": len(alerts),
        "sms_sent": sent_count,
        "sms_failed": failed_count,
        "provider": dispatcher.provider_name,
        "dispatch_mode": getattr(dispatcher, "dispatch_mode", "auto"),
        "dry_run": dispatcher.dry_run,
        "db_enabled": dispatcher.db_manager.is_mssql_enabled(),
        "queue_depth": dispatcher.queue.qsize(),
        "recipients_count": len(dispatcher.router.phone_numbers),
    }), 200


@app.route("/api/alerts", methods=["GET"])
def api_alerts():
    """Retrieve recent alerts for visual dashboard table."""
    try:
        limit = max(1, min(200, int(request.args.get("limit", 25))))
    except ValueError:
        limit = 25
    return jsonify(dispatcher.db_manager.get_recent_alerts(limit=limit)), 200


@app.route("/api/sms-logs", methods=["GET"])
def api_sms_logs():
    """Retrieve recent SMS logs for visual dashboard table."""
    try:
        limit = max(1, min(200, int(request.args.get("limit", 25))))
    except ValueError:
        limit = 25
    return jsonify(dispatcher.db_manager.get_recent_sms_logs(limit=limit)), 200


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """Read or update configuration visually."""
    global config
    if request.method == "GET":
        return jsonify(config), 200

    new_cfg = request.get_json()
    if not isinstance(new_cfg, dict):
        return jsonify({"status": "error", "message": "Expected JSON configuration object"}), 400

    try:
        save_config(new_cfg, CONFIG_FILE_PATH)
        config = new_cfg
        dispatcher.update_config(new_cfg)
        log.info("Configuration updated and reloaded via Visual Panel.")
        return jsonify({"status": "ok", "message": "Configuration saved and applied successfully"}), 200
    except Exception as e:
        log.exception("Failed to save configuration from Visual Panel: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/dispatch-mode", methods=["POST"])
def api_set_dispatch_mode():
    """Quickly switch between 'auto' and 'manual' dispatch mode without reloading whole page."""
    global config
    data = request.get_json() or {}
    new_mode = str(data.get("dispatch_mode", "")).strip().lower()
    if new_mode not in ("auto", "manual"):
        return jsonify({"success": False, "message": "Invalid dispatch_mode. Must be 'auto' or 'manual'."}), 400

    try:
        if "sms" not in config:
            config["sms"] = {}
        config["sms"]["dispatch_mode"] = new_mode
        save_config(config, CONFIG_FILE_PATH)
        dispatcher.update_config(config)
        log.info("Dispatch mode switched to '%s' via Quick Switch API.", new_mode)
        return jsonify({
            "success": True,
            "dispatch_mode": new_mode,
            "message": f"حالت ارسال به '{'خودکار' if new_mode == 'auto' else 'دستی'}' تغییر یافت."
        }), 200
    except Exception as e:
        log.exception("Error changing dispatch mode: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/provider-mode", methods=["POST"])
def api_set_provider_mode():
    """Quickly switch SMS provider between 'http' (real) and 'mock' (simulation)."""
    global config
    data = request.get_json() or {}
    new_provider = str(data.get("provider", "")).strip().lower()
    if new_provider not in ("http", "mock"):
        return jsonify({"success": False, "message": "Invalid provider. Must be 'http' or 'mock'."}), 400

    try:
        if "sms" not in config:
            config["sms"] = {}
        config["sms"]["provider"] = new_provider
        save_config(config, CONFIG_FILE_PATH)
        dispatcher.update_config(config)
        log.info("SMS Provider switched to '%s' via Quick Switch API.", new_provider)
        return jsonify({
            "success": True,
            "provider": new_provider,
            "message": f"درگاه پیامک به '{'ارسال واقعی (HTTP)' if new_provider == 'http' else 'شبیه‌ساز (Mock)'}' تغییر یافت."
        }), 200
    except Exception as e:
        log.exception("Error changing provider mode: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/test/sms", methods=["POST"])
def api_test_sms():
    """Send immediate test SMS directly to configured SMS gateway or provider."""
    data = request.get_json() or {}
    phone = str(data.get("phone", "")).strip()
    msg = str(data.get("message", "تست ارسال پیامک")).strip()

    if not phone:
        return jsonify({"success": False, "message": "شماره موبایل الزامی است."}), 400

    try:
        # Determine provider to test against:
        # 1. Custom gateway configuration passed directly from Visual Panel
        gateway_override = data.get("gateway_config")
        if gateway_override and isinstance(gateway_override, dict):
            from adapters.http import HttpSmsProvider
            test_provider = HttpSmsProvider(gateway_override)
            provider_label = "http (درگاه مستقیم)"
        elif data.get("force_http") or data.get("provider") == "http":
            from adapters.http import HttpSmsProvider
            http_cfg = config.get("sms", {}).get("http", {}) if config else {}
            test_provider = HttpSmsProvider(http_cfg)
            provider_label = "http"
        else:
            test_provider = dispatcher.sms_provider
            provider_label = dispatcher.provider_name

        res = test_provider.send(phone, msg)
        status_str = "sent" if res.success else "failed"

        # Log manual test to database
        dispatcher.db_manager.save_sms_log(
            alert_name="MANUAL_TEST_SMS",
            target="Panel User",
            phone=phone,
            message_text=msg,
            provider=provider_label,
            status=status_str,
            response_code=res.response_code,
            raw_response=res.raw_response,
            error_details=res.error_message,
        )

        return jsonify({
            "success": res.success,
            "provider_used": provider_label,
            "status_code": res.status_code,
            "response_code": res.response_code,
            "message_id": res.message_id,
            "raw_response": res.raw_response,
            "error_message": res.error_message,
            "phone": phone,
            "message": "پیامک با موفقیت توسط درگاه پذیرفته شد." if res.success else (res.error_message or "درگاه پیامک با خطا پاسخ داد."),
        }), 200
    except Exception as e:
        log.exception("Error executing manual test SMS: %s", e)
        return jsonify({"success": False, "message": str(e)}), 200


@app.route("/api/alerts/send-manual", methods=["POST"])
def api_alerts_send_manual():
    """Manually send a specific alert from the table to a specified phone number."""
    data = request.get_json() or {}
    phone = str(data.get("phone", "")).strip()
    msg = str(data.get("message", "")).strip()
    alert_name = str(data.get("alert_name", "هشدار دستی")).strip()
    alert_id = str(data.get("alert_id", "")).strip()

    if not phone:
        return jsonify({"success": False, "message": "شماره موبایل گیرنده الزامی است."}), 400
    if not msg:
        msg = f"هشدار دستی ردگیت: {alert_name}"

    try:
        res = dispatcher.sms_provider.send(phone, msg)
        status_str = "sent" if res.success else "failed"

        dispatcher.db_manager.save_sms_log(
            alert_name=alert_name or "MANUAL_DISPATCH",
            target=phone,
            phone=phone,
            message_text=msg,
            provider=dispatcher.provider_name,
            status=status_str,
            response_code=res.response_code,
            raw_response=res.raw_response,
            error_details=res.error_message,
            alert_id=alert_id or None,
        )

        return jsonify({
            "success": res.success,
            "phone": phone,
            "status_code": res.status_code,
            "response_code": res.response_code,
            "message_id": res.message_id,
            "raw_response": res.raw_response,
            "error_message": res.error_message,
            "message": "پیامک این هشدار با موفقیت ارسال شد." if res.success else (res.error_message or "ارسال ناموفق بود."),
        }), 200
    except Exception as e:
        log.exception("Error executing manual alert send: %s", e)
        return jsonify({"success": False, "message": str(e)}), 200


@app.route("/api/test/db", methods=["POST"])
def api_test_db():
    """Test SQL Server connectivity with provided parameters."""
    try:
        data = request.get_json() or {}
        res = dispatcher.db_manager.test_mssql_connection(override_cfg=data)
        return jsonify(res), 200
    except Exception as e:
        log.exception("Error in /api/test/db: %s", e)
        return jsonify({"success": False, "message": str(e)}), 200


@app.route("/api/test/db/init", methods=["POST"])
def api_test_db_init():
    """Create or verify alert and SMS tables in SQL Server."""
    try:
        data = request.get_json() or {}
        res = dispatcher.db_manager.ensure_mssql_database_and_tables(override_cfg=data)
        return jsonify(res), 200
    except Exception as e:
        log.exception("Error in /api/test/db/init: %s", e)
        return jsonify({"success": False, "message": str(e)}), 200


@app.route("/api/logs", methods=["GET"])
def api_logs():
    """Retrieve tail of service log file."""
    try:
        lines_count = max(10, min(500, int(request.args.get("lines", 100))))
    except ValueError:
        lines_count = 100

    log_file = config.get("logging", {}).get("file", "logs/relay.log") if config else "logs/relay.log"
    if not os.path.isabs(log_file):
        log_file = os.path.join(BASE_DIR, log_file)

    if not os.path.exists(log_file):
        return "(Log file does not exist yet.)", 200, {"Content-Type": "text/plain; charset=utf-8"}

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            tail = lines[-lines_count:]
            return "".join(tail), 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception as e:
        return f"Error reading log file: {e}", 500, {"Content-Type": "text/plain; charset=utf-8"}


# ==============================================================================
# Webhook and Diagnostic Endpoints
# ==============================================================================

@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint providing status, queue size, and active provider."""
    auth_enabled = bool(config.get("server", {}).get("auth_token"))
    return jsonify({
        "status": "healthy",
        "service": "redgate-sms-relay",
        "dry_run": dispatcher.dry_run,
        "provider": dispatcher.provider_name,
        "queue_depth": dispatcher.queue.qsize(),
        "auth_token_required": auth_enabled,
        "sqlite_database": dispatcher.audit_logger.db_path,
        "mssql_enabled": dispatcher.db_manager.is_mssql_enabled(),
    }), 200


@app.route("/webhook/redgate", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def webhook_redgate():
    """Receive Redgate webhook alert payload."""
    source_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    raw_text = request.get_data(as_text=True) or ""

    # 1. Constant-time token verification
    if not _is_authenticated():
        saved = _save_raw_dump(source_ip, raw_text, prefix="unauthorized")
        log.warning("Unauthorized webhook request from %s (saved to %s)", source_ip, saved)
        return jsonify({
            "status": "error",
            "message": "Invalid or missing webhook authorization token"
        }), 401

    # 2. Reject empty bodies
    if not raw_text.strip():
        saved = _save_raw_dump(source_ip, raw_text, prefix="empty")
        log.warning("Rejected empty webhook payload from %s (saved to %s)", source_ip, saved)
        return jsonify({
            "status": "error",
            "message": "Payload body is empty"
        }), 400

    # 3. Validate JSON structure strictly
    try:
        parsed_payload = json.loads(raw_text)
    except json.JSONDecodeError as e:
        saved = _save_raw_dump(source_ip, raw_text, prefix="malformed")
        log.error("Rejected malformed JSON from %s (saved to %s): %s", source_ip, saved, e)
        return jsonify({
            "status": "error",
            "message": f"Malformed JSON payload: {e.msg} at line {e.lineno}"
        }), 400

    # 4. Save raw payload and queue non-blockingly
    try:
        raw_saved_file = dispatcher.enqueue_alert(
            source_ip=source_ip,
            raw_payload=raw_text,
            parsed_payload=parsed_payload,
        )
    except RuntimeError as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 503

    log.info("Accepted alert from %s, saved to %s, queued for processing.", source_ip, raw_saved_file)

    return jsonify({
        "status": "accepted",
        "message": "Alert received and queued for dispatch",
        "raw_file": raw_saved_file,
    }), 202


@app.route("/audit/recent", methods=["GET"])
def audit_recent():
    """Inspect recent SMS dispatch records (secured if auth_token is configured)."""
    if not _is_authenticated():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    limit_param = request.args.get("limit", "20")
    try:
        limit = max(1, min(100, int(limit_param)))
    except ValueError:
        limit = 20

    return jsonify(dispatcher.db_manager.get_recent_sms_logs(limit=limit)), 200


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    config_file = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    if not os.path.isabs(config_file):
        config_file = os.path.join(BASE_DIR, config_file)

    init_service(config_file)

    host = config.get("server", {}).get("host", "0.0.0.0")
    port = int(config.get("server", {}).get("port", 8000))
    provider = config.get("sms", {}).get("provider", "mock")
    phones = config.get("phone_numbers", [])
    db_enabled = config.get("database", {}).get("enabled", False)

    dispatch_mode = config.get("sms", {}).get("dispatch_mode", "auto")

    print("\n" + "=" * 70)
    print(" [OK] Redgate Alert -> SMS Relay Service is RUNNING!")
    print("=" * 70)
    print(f" * Dashboard URL:          http://localhost:{port}")
    print(f" * Redgate Webhook:        http://localhost:{port}/webhook/redgate")
    print(f" * SMS Gateway Mode:       {provider.upper()} ({'REAL SMS GATEWAY' if provider == 'http' else 'MOCK SIMULATOR'})")
    print(f" * Dispatch Mode:          {dispatch_mode.upper()} ({'AUTOMATIC' if dispatch_mode == 'auto' else 'MANUAL / DEV'})")
    print(f" * Recipient Numbers:      {', '.join(phones) if phones else '(No phone numbers registered)'}")
    print(f" * SQL Server Storage:     {'ENABLED' if db_enabled else 'DISABLED (SQLite fallback)'}")
    print("=" * 70)
    print(f" Browser opened automatically: http://localhost:{port}\n")

    try:
        from waitress import serve
        serve(app, host=host, port=port)
    except ImportError:
        app.run(host=host, port=port)


if __name__ == "__main__":
    main()
