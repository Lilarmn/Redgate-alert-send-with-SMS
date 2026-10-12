"""Generic, highly configurable HTTP SMS Provider.

Supports:
- Arbitrary HTTP method (POST, PUT, GET).
- Configurable URL and custom headers.
- Configurable auth mechanism:
  - Bearer token: {"auth_type": "bearer", "api_key": "..."}
  - Custom header: {"auth_type": "header", "header_name": "X-API-KEY", "header_value": "..."}
  - Basic auth or query param.
- Configurable JSON or form-urlencoded request body templates with placeholders:
  "{destination}", "{message}", "{phone}".
- Configurable success evaluation:
  - By HTTP status code (2xx).
  - By JSON field condition (e.g. key "success" == true, or key "code" == "SENT_OK").
- TLS/SSL settings:
  - Optional custom CA bundle path (ca_bundle: "/path/to/ca.pem").
  - Optional verify_ssl: true/false.
- Exponential backoff with configurable retry limits.
- Safe phone masking in all logs.
"""
import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from adapters.base import BaseSmsProvider, SendResult

log = logging.getLogger("sms.http")


def mask_phone(phone: str) -> str:
    phone = str(phone).strip()
    if len(phone) < 7:
        return "***"
    return f"{phone[:4]}***{phone[-4:]}"


class HttpSmsProvider(BaseSmsProvider):
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.url = config.get("url", "https://api.sms-gateway.example.com/v1/sms/send")
        self.method = config.get("method", "POST").upper()
        self.timeout = float(config.get("timeout_seconds", 10.0))
        self.max_retries = int(config.get("max_retries", 3))
        self.retry_delay = float(config.get("retry_delay_seconds", 1.0))

        # Authentication
        self.auth_type = config.get("auth_type", "bearer").lower()
        self.api_key = config.get("api_key", "")
        self.auth_header_name = config.get("auth_header_name", "Authorization")

        # Headers
        self.headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept-Language": config.get("accept_language", "fa"),
        }
        if config.get("headers"):
            self.headers.update(config["headers"])

        # Set auth header
        if self.auth_type == "bearer" and self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.auth_type == "header" and self.api_key:
            self.headers[self.auth_header_name] = self.api_key

        # Request Body Template
        # Default matches standard REST SMS Gateway: {"destination": "{destination}", "message": "{message}"}
        self.body_template = config.get("body_template", {
            "destination": "{destination}",
            "message": "{message}",
        })

        # Success condition: {"key": "success", "expected": true}
        self.success_condition = config.get("success_condition", {
            "key": "success",
            "expected": True,
        })

        # TLS / SSL Context
        self.ssl_context = None
        verify_ssl = config.get("verify_ssl", True)
        ca_bundle = config.get("ca_bundle")
        if ca_bundle:
            self.ssl_context = ssl.create_default_context(cafile=ca_bundle)
        elif not verify_ssl:
            self.ssl_context = ssl._create_unverified_context()
        else:
            self.ssl_context = ssl.create_default_context()

    def _build_payload(self, phone: str, text: str) -> bytes:
        if isinstance(self.body_template, dict):
            # Format dictionary values
            built_dict = {}
            for k, v in self.body_template.items():
                if isinstance(v, str):
                    built_dict[k] = v.format(destination=phone, phone=phone, message=text)
                else:
                    built_dict[k] = v
            return json.dumps(built_dict, ensure_ascii=False).encode("utf-8")
        elif isinstance(self.body_template, str):
            formatted_str = self.body_template.format(destination=phone, phone=phone, message=text)
            return formatted_str.encode("utf-8")
        return json.dumps({"destination": phone, "message": text}, ensure_ascii=False).encode("utf-8")

    def _evaluate_success(self, status_code: int, resp_json: Any) -> bool:
        if not (200 <= status_code < 300):
            return False

        if isinstance(resp_json, dict) and self.success_condition:
            key = self.success_condition.get("key")
            expected = self.success_condition.get("expected")
            if key in resp_json:
                return resp_json[key] == expected

        return 200 <= status_code < 300

    def send(self, phone: str, text: str) -> SendResult:
        encoded_data = self._build_payload(phone, text)
        masked = mask_phone(phone)
        last_error = None

        for attempt in range(1, self.max_retries + 1):
            req = urllib.request.Request(
                self.url,
                data=encoded_data,
                headers=self.headers,
                method=self.method,
            )
            try:
                log.info("Sending SMS attempt %d/%d to %s", attempt, self.max_retries, masked)
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as resp:
                    status_code = resp.status
                    raw_body = resp.read().decode("utf-8", errors="replace")

                    resp_json = None
                    try:
                        resp_json = json.loads(raw_body)
                    except Exception:
                        resp_json = {}

                    if not isinstance(resp_json, dict):
                        resp_json = {"raw": resp_json}

                    success = self._evaluate_success(status_code, resp_json)
                    code = resp_json.get("code", "SENT_OK" if success else "UNKNOWN")
                    msg_id = None
                    if isinstance(resp_json.get("data"), dict):
                        messages = resp_json["data"].get("messages")
                        if isinstance(messages, list) and len(messages) > 0 and isinstance(messages[0], dict):
                            msg_id = messages[0].get("id")

                    return SendResult(
                        success=success,
                        status_code=status_code,
                        response_code=str(code),
                        message_id=msg_id,
                        error_message=resp_json.get("message") if not success else None,
                        raw_response=raw_body,
                        extra_details=resp_json,
                    )

            except urllib.error.HTTPError as e:
                status_code = e.code
                raw_body = e.read().decode("utf-8", errors="replace")
                resp_json = {}
                try:
                    resp_json = json.loads(raw_body)
                    if not isinstance(resp_json, dict):
                        resp_json = {"raw": resp_json}
                except Exception:
                    pass

                code = resp_json.get("code", "HTTP_ERROR")
                err_msg = resp_json.get("message", str(e))

                # Do not retry 4xx errors except 429 Rate limit
                if 400 <= status_code < 500 and status_code != 429:
                    log.error("Client error (%d) sending SMS to %s: %s", status_code, masked, raw_body)
                    return SendResult(
                        success=False,
                        status_code=status_code,
                        response_code=str(code),
                        error_message=err_msg,
                        raw_response=raw_body,
                        extra_details=resp_json,
                    )

                last_error = f"HTTP {status_code}: {err_msg}"
                log.warning("HTTP error %d on attempt %d: %s. Will retry...", status_code, attempt, err_msg)

            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = f"Network/Socket error: {e}"
                log.warning("Connection failure on attempt %d for %s: %s", attempt, masked, e)

            # Wait with exponential backoff before next retry
            if attempt < self.max_retries:
                sleep_time = self.retry_delay * (2 ** (attempt - 1))
                time.sleep(sleep_time)

        return SendResult(
            success=False,
            status_code=None,
            response_code="RETRIES_EXHAUSTED",
            error_message=f"Failed after {self.max_retries} attempts: {last_error}",
            raw_response=None,
        )
