"""Mock SMS Provider for local development, testing, and simulation."""
import datetime
import json
import logging
import os
import time
from typing import Any, Dict

from adapters.base import BaseSmsProvider, SendResult

log = logging.getLogger("sms.mock")


def mask_phone(phone: str) -> str:
    phone = str(phone).strip()
    if len(phone) < 7:
        return "***"
    return f"{phone[:4]}***{phone[-4:]}"


class MockProvider(BaseSmsProvider):
    """Simulator provider that writes messages to console and/or file,
    supporting simulated latency, failures, and timeouts.
    """

    def __init__(self, config: Dict[str, Any]):
        self.output_file = config.get("output_file")
        self.simulate_latency_seconds = float(config.get("simulate_latency_seconds", 0.0))
        self.simulate_failure = bool(config.get("simulate_failure", False))
        self.simulate_timeout = bool(config.get("simulate_timeout", False))
        self.counter = 0

        if self.output_file:
            os.makedirs(os.path.dirname(os.path.abspath(self.output_file)), exist_ok=True)

    def send(self, phone: str, text: str) -> SendResult:
        self.counter += 1
        msg_id = f"mock-msg-{int(time.time())}-{self.counter}"
        masked = mask_phone(phone)

        if self.simulate_latency_seconds > 0:
            time.sleep(self.simulate_latency_seconds)

        if self.simulate_timeout:
            log.warning("Simulated timeout occurred for phone=%s", masked)
            # Simulate socket timeout delay if configured
            if self.simulate_latency_seconds > 0:
                time.sleep(self.simulate_latency_seconds)
            return SendResult(
                success=False,
                status_code=408,
                response_code="TIMEOUT",
                error_message="Simulated connection/read timeout",
                raw_response='{"success": false, "code": "TIMEOUT"}',
            )

        if self.simulate_failure:
            log.warning("Simulated failure occurred for phone=%s", masked)
            return SendResult(
                success=False,
                status_code=500,
                response_code="SIMULATED_ERROR",
                error_message="Mock provider failure configured",
                raw_response='{"success": false, "code": "SIMULATED_ERROR"}',
            )

        log.info("[MOCK SMS SENT] -> %s | Len=%d | Text: %s", masked, len(text), text)

        record = {
            "timestamp": datetime.datetime.now().isoformat(),
            "message_id": msg_id,
            "destination": masked,
            "text": text,
            "length": len(text),
        }

        if self.output_file:
            with open(self.output_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return SendResult(
            success=True,
            status_code=200,
            response_code="SENT_OK",
            message_id=msg_id,
            raw_response=json.dumps({"success": True, "code": "SENT_OK", "id": msg_id}),
        )
