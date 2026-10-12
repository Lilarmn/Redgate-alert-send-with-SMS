"""Structured Audit Logger for Redgate Alert -> SMS Relay.

Logs each alert and individual delivery attempt:
1. Standard rotating text log files (never logging API keys or unmasked phones).
2. Structured SQLite database or JSONL audit trail storing raw responses
   and masked phones by default.
"""
import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
import threading
from typing import Any, Dict, Optional

from router import mask_phone


class AuditLogger:
    def __init__(self, config: Dict[str, Any], base_dir: Optional[str] = None):
        self.config = config
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.lock = threading.Lock()

        # 1. Setup rotating text logger
        log_cfg = config.get("logging", {})
        log_file = log_cfg.get("file", "logs/relay.log")
        if not os.path.isabs(log_file):
            log_file = os.path.join(self.base_dir, log_file)
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)

        self.logger = logging.getLogger("relay.audit")
        self.logger.setLevel(logging.INFO)

        # Configure root or shared handler if not already attached
        root_logger = logging.getLogger()
        if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=int(log_cfg.get("max_bytes", 10 * 1024 * 1024)),
                backupCount=int(log_cfg.get("backup_count", 5)),
                encoding="utf-8",
            )
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
            root_logger.setLevel(logging.INFO)

        # 2. Setup SQLite Audit DB
        storage_cfg = config.get("storage", {})
        self.db_path = storage_cfg.get("database_path", "data/audit.db")
        if not os.path.isabs(self.db_path):
            self.db_path = os.path.join(self.base_dir, self.db_path)
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_sqlite()

        # 3. Setup JSONL audit file
        self.jsonl_file = log_cfg.get("audit_jsonl_file", "logs/audit.jsonl")
        if not os.path.isabs(self.jsonl_file):
            self.jsonl_file = os.path.join(self.base_dir, self.jsonl_file)
        os.makedirs(os.path.dirname(os.path.abspath(self.jsonl_file)), exist_ok=True)

    def _init_sqlite(self) -> None:
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    alert_id TEXT,
                    alert_name TEXT,
                    severity TEXT,
                    target TEXT,
                    recipient_phone_masked TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_code TEXT,
                    provider_message_id TEXT,
                    raw_response TEXT,
                    error_details TEXT
                )
            """)
            # Ensure raw_response column exists for existing DBs
            cur.execute("PRAGMA table_info(audit_logs)")
            existing_cols = {row[1] for row in cur.fetchall()}
            if "raw_response" not in existing_cols:
                try:
                    cur.execute("ALTER TABLE audit_logs ADD COLUMN raw_response TEXT")
                except Exception:
                    pass

            cur.execute("CREATE INDEX IF NOT EXISTS ix_audit_status ON audit_logs (status)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_audit_phone ON audit_logs (recipient_phone_masked)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_audit_time ON audit_logs (timestamp)")
            conn.commit()
            conn.close()

    def log_delivery(
        self,
        alert_details: Dict[str, str],
        phone: str,
        message_text: str,
        provider_name: str,
        status: str,  # 'sent', 'failed', 'skipped_quiet_hours', 'rate_limited', 'dry_run', 'exception'
        response_code: Optional[str] = None,
        provider_message_id: Optional[str] = None,
        raw_response: Optional[str] = None,
        error_details: Optional[str] = None,
    ) -> None:
        now_iso = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        masked = mask_phone(phone) if phone not in ("ALL", "NONE") else phone

        self.logger.info(
            "DELIVERY [%s] alert='%s' target='%s' to=%s status=%s provider=%s id=%s code=%s err=%s",
            status.upper(),
            alert_details.get("alert_name", "-"),
            alert_details.get("target", "-"),
            masked,
            status,
            provider_name,
            provider_message_id or "-",
            response_code or "-",
            error_details or "-",
        )

        record = {
            "timestamp": now_iso,
            "alert_id": alert_details.get("alert_id"),
            "alert_name": alert_details.get("alert_name"),
            "severity": alert_details.get("severity"),
            "target": alert_details.get("target"),
            "phone_masked": masked,
            "message": message_text,
            "provider": provider_name,
            "status": status,
            "response_code": response_code,
            "provider_message_id": provider_message_id,
            "raw_response": raw_response,
            "error_details": error_details,
        }

        with self.lock:
            # 1. SQLite Insert
            try:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO audit_logs (
                        timestamp, alert_id, alert_name, severity, target,
                        recipient_phone_masked, message_text,
                        provider, status, response_code, provider_message_id,
                        raw_response, error_details
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now_iso,
                    alert_details.get("alert_id"),
                    alert_details.get("alert_name"),
                    alert_details.get("severity"),
                    alert_details.get("target"),
                    masked,
                    message_text,
                    provider_name,
                    status,
                    response_code,
                    provider_message_id,
                    raw_response,
                    error_details,
                ))
                conn.commit()
                conn.close()
            except Exception as e:
                self.logger.error("Failed to insert audit record to SQLite: %s", e)

            # 2. JSONL write
            try:
                with open(self.jsonl_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as e:
                self.logger.error("Failed to write to JSONL audit log: %s", e)
