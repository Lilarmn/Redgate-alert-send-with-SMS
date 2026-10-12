"""Background Worker and Dispatcher.

Features:
- Non-blocking queue worker.
- Isolated per-recipient try/except blocks ensuring one failed recipient
  or exception never prevents other recipients from receiving alerts.
- Dedicated audit log entries for every recipient even on exceptions.
- Accurate rate-limiting & de-duplication accounting (only marks success on actual delivery).
- Message splitting / multi-part dispatch support.
"""
import datetime
import logging
import os
import queue
import threading
import time
from typing import Any, Dict

from adapters.factory import get_sms_provider
from audit_logger import AuditLogger
from db_manager import DatabaseManager
from extractor import extract_alert_details
from router import AlertRouter, format_sms_message, mask_phone

log = logging.getLogger("relay.dispatcher")


class AlertDispatcher:
    def __init__(self, config: Dict[str, Any], base_dir: str = None):
        self.config = config
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))

        raw_dir = config.get("storage", {}).get("raw_payloads_dir", "data/raw_payloads")
        if not os.path.isabs(raw_dir):
            raw_dir = os.path.join(self.base_dir, raw_dir)
        self.raw_dir = raw_dir
        os.makedirs(self.raw_dir, exist_ok=True)

        self.field_mappings = config.get("field_mappings", {})
        phone_numbers = config.get("phone_numbers", [])
        self.router = AlertRouter(config.get("routing", {}), phone_numbers=phone_numbers)
        self.templates_config = config.get("templates", {})
        self.dry_run = bool(config.get("dry_run", False))

        self.sms_provider = get_sms_provider(config.get("sms", {}))
        self.provider_name = config.get("sms", {}).get("provider", "mock")
        self.dispatch_mode = config.get("sms", {}).get("dispatch_mode", "auto").lower()
        self.audit_logger = AuditLogger(config, base_dir=self.base_dir)
        self.db_manager = DatabaseManager(config, base_dir=self.base_dir)

        # Sync phone numbers from database if available
        db_phones = self.db_manager.get_active_phones_from_db()
        for p in db_phones:
            if p not in self.router.phone_numbers:
                self.router.phone_numbers.append(p)

        # Asynchronous processing queue
        self.queue: queue.Queue = queue.Queue(maxsize=10000)
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

    def update_config(self, new_config: Dict[str, Any]) -> None:
        """Dynamically apply updated configuration without stopping the worker."""
        self.config = new_config
        self.field_mappings = new_config.get("field_mappings", self.field_mappings)
        phone_numbers = new_config.get("phone_numbers", [])
        self.router = AlertRouter(new_config.get("routing", {}), phone_numbers=phone_numbers)
        self.templates_config = new_config.get("templates", {})
        self.dry_run = bool(new_config.get("dry_run", False))
        self.sms_provider = get_sms_provider(new_config.get("sms", {}))
        self.provider_name = new_config.get("sms", {}).get("provider", "mock")
        self.dispatch_mode = new_config.get("sms", {}).get("dispatch_mode", "auto").lower()
        self.db_manager.update_config(new_config)

        # Sync phones to DB and pull any DB phones
        for p in self.router.phone_numbers:
            self.db_manager.sync_phone_to_db(p)
        for p in self.db_manager.get_active_phones_from_db():
            if p not in self.router.phone_numbers:
                self.router.phone_numbers.append(p)

        log.info("Dispatcher config updated dynamically. Active phones: %s", self.router.phone_numbers)

    def enqueue_alert(self, source_ip: str, raw_payload: str, parsed_payload: Any) -> str:
        """Saves raw payload to disk immediately and queues alert for async delivery.
        
        Uses non-blocking put with exception logging if queue is saturated.
        """
        now = datetime.datetime.now()
        timestamp_str = now.strftime("%Y%m%d_%H%M%S_%f")
        raw_filename = f"alert_{timestamp_str}.json"
        raw_path = os.path.join(self.raw_dir, raw_filename)

        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(raw_payload)

        try:
            self.queue.put_nowait({
                "source_ip": source_ip,
                "raw_filename": raw_filename,
                "raw_payload": raw_payload,
                "parsed_payload": parsed_payload,
                "received_at": now.isoformat(),
            })
        except queue.Full:
            log.error("Internal processing queue is full! Alert %s queued failed.", raw_filename)
            raise RuntimeError("Internal processing queue saturated")

        return raw_filename

    def _worker_loop(self) -> None:
        while True:
            item = self.queue.get()
            try:
                self._process_item(item)
            except Exception as e:
                log.exception("Unhandled error processing alert item from queue: %s", e)
            finally:
                self.queue.task_done()

    def _process_item(self, item: Dict[str, Any]) -> None:
        parsed_payload = item["parsed_payload"]
        details = extract_alert_details(parsed_payload, self.field_mappings)
        if not details.get("timestamp"):
            details["timestamp"] = item["received_at"]

        alert_name = details.get("alert_name")
        target = details.get("target")
        severity = details.get("severity")
        status = details.get("status")

        log.info("Processing alert '%s' on '%s' [sev=%s, status=%s]", alert_name, target, severity, status)

        # Persist alert record into Database (SQL Server and SQLite fallback)
        alert_db_id = None
        try:
            alert_db_id = self.db_manager.save_alert(
                details=details,
                raw_payload=item.get("raw_payload", ""),
                source_ip=item.get("source_ip", ""),
            )
        except Exception as e:
            log.error("Failed to save alert to database: %s", e)

        # 1. De-duplication check (status-aware)
        if self.router.is_deduplicated(details):
            log.info("Deduplication matched for alert '%s' on '%s' [%s]. Suppressing dispatch.", alert_name, target, status)
            self.audit_logger.log_delivery(
                alert_details=details,
                phone="ALL",
                message_text="(Deduplicated alert suppressed)",
                provider_name=self.provider_name,
                status="deduplicated",
            )
            return

        # 2. Routing rule evaluation
        recipients, matched_rules = self.router.route_alert(details)
        if not recipients:
            log.warning("No routing rules or recipients matched for alert '%s'.", alert_name)
            self.audit_logger.log_delivery(
                alert_details=details,
                phone="NONE",
                message_text="(No recipients matched routing rules)",
                provider_name=self.provider_name,
                status="no_recipient",
            )
            return

        log.info("Alert matched rules %s with %d recipients.", matched_rules, len(recipients))

        # 3. Format SMS messages (can return multiple if split_messages is enabled)
        sms_parts = format_sms_message(details, self.templates_config)
        msg_preview = sms_parts[0] if sms_parts else ""

        # Check if in Manual Mode (Development / Staging mode)
        if self.dispatch_mode == "manual":
            log.info("Manual Mode active: Alert '%s' on '%s' saved to DB. Automatic SMS dispatch suppressed.", alert_name, target)
            self.audit_logger.log_delivery(
                alert_details=details,
                phone="MANUAL_MODE",
                message_text=msg_preview,
                provider_name=self.provider_name,
                status="saved_manual_mode",
                error_details="Automatic dispatch suppressed (Manual/Dev Mode active). Use manual send button to test.",
            )
            self.db_manager.save_sms_log(
                alert_name=alert_name,
                target=target,
                phone="MANUAL_MODE",
                message_text=msg_preview,
                provider=self.provider_name,
                status="manual_mode_ready",
                error_details="Manual mode active - alert stored, ready for manual test/dispatch",
                alert_id=alert_db_id or details.get("alert_id"),
            )
            return

        # 4. Process each recipient with isolated try/except block
        is_quiet = self.router.is_quiet_hours()
        any_success = False

        for rec in recipients:
            phone = rec.get("phone")
            name = rec.get("name", "Unknown")
            bypass_quiet = rec.get("bypass_quiet_hours", False)
            masked = mask_phone(phone)

            try:
                # Check quiet hours
                if is_quiet and not bypass_quiet:
                    log.info("Quiet hours active. Skipping SMS for %s (%s)", name, masked)
                    self.audit_logger.log_delivery(
                        alert_details=details,
                        phone=phone,
                        message_text=sms_parts[0],
                        provider_name=self.provider_name,
                        status="skipped_quiet_hours",
                        error_details="Suppressed due to quiet hours schedule",
                    )
                    self.db_manager.save_sms_log(
                        alert_name=alert_name,
                        target=target,
                        phone=phone,
                        message_text=sms_parts[0],
                        provider=self.provider_name,
                        status="skipped_quiet_hours",
                        error_details="Suppressed due to quiet hours",
                        alert_id=alert_db_id or details.get("alert_id"),
                    )
                    continue

                # Check rate limiting per phone
                if self.router.is_rate_limited(phone):
                    log.warning("Rate limit exceeded for %s (%s). Skipping SMS.", name, masked)
                    self.audit_logger.log_delivery(
                        alert_details=details,
                        phone=phone,
                        message_text=sms_parts[0],
                        provider_name=self.provider_name,
                        status="rate_limited",
                        error_details="Exceeded hourly rate limit for this phone number",
                    )
                    self.db_manager.save_sms_log(
                        alert_name=alert_name,
                        target=target,
                        phone=phone,
                        message_text=sms_parts[0],
                        provider=self.provider_name,
                        status="rate_limited",
                        error_details="Exceeded hourly rate limit",
                        alert_id=alert_db_id or details.get("alert_id"),
                    )
                    continue

                # Dry-run mode
                if self.dry_run:
                    log.info("[DRY-RUN] Would send to %s (%s) | %d part(s)", name, masked, len(sms_parts))
                    for part_text in sms_parts:
                        self.audit_logger.log_delivery(
                            alert_details=details,
                            phone=phone,
                            message_text=part_text,
                            provider_name=self.provider_name,
                            status="dry_run",
                            response_code="DRY_RUN_OK",
                        )
                        self.db_manager.save_sms_log(
                            alert_name=alert_name,
                            target=target,
                            phone=phone,
                            message_text=part_text,
                            provider=self.provider_name,
                            status="dry_run",
                            response_code="DRY_RUN_OK",
                            alert_id=alert_db_id or details.get("alert_id"),
                        )
                    continue

                # Dispatch each part via SMS Provider
                all_parts_ok = True
                for part_text in sms_parts:
                    res = self.sms_provider.send(phone, part_text)
                    status_str = "sent" if res.success else "failed"

                    self.audit_logger.log_delivery(
                        alert_details=details,
                        phone=phone,
                        message_text=part_text,
                        provider_name=self.provider_name,
                        status=status_str,
                        response_code=res.response_code,
                        provider_message_id=res.message_id,
                        raw_response=res.raw_response,
                        error_details=res.error_message,
                    )
                    self.db_manager.save_sms_log(
                        alert_name=alert_name,
                        target=target,
                        phone=phone,
                        message_text=part_text,
                        provider=self.provider_name,
                        status=status_str,
                        response_code=res.response_code,
                        raw_response=res.raw_response,
                        error_details=res.error_message,
                        alert_id=alert_db_id or details.get("alert_id"),
                    )
                    if res.success:
                        self.router.record_send_for_rate_limit(phone)
                    else:
                        all_parts_ok = False

                if all_parts_ok:
                    any_success = True

            except Exception as e:
                log.exception("Unexpected error dispatching alert to recipient %s (%s): %s", name, masked, e)
                self.audit_logger.log_delivery(
                    alert_details=details,
                    phone=phone,
                    message_text=sms_parts[0] if sms_parts else "",
                    provider_name=self.provider_name,
                    status="exception",
                    error_details=f"Unexpected exception in dispatcher: {e}",
                )
                self.db_manager.save_sms_log(
                    alert_name=alert_name,
                    target=target,
                    phone=phone,
                    message_text=sms_parts[0] if sms_parts else "",
                    provider=self.provider_name,
                    status="exception",
                    error_details=f"Unexpected exception in dispatcher: {e}",
                    alert_id=alert_db_id or details.get("alert_id"),
                )

        # Mark dedup only if at least one delivery succeeded (or dry-run),
        # so gateway network outages don't prevent subsequent retried alerts from being processed.
        if any_success or self.dry_run:
            self.router.record_dedup_success(details)
