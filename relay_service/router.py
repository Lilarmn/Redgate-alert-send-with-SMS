"""Alert Router, Message Formatter, and Delivery State Evaluator.

Features:
- Routing rules matching severity, alert_name, and target (wildcard support).
- Status-aware deduplication window (differentiates Raised vs Ended/Cleared).
- TTL-based pruning of de-duplication cache so memory never leaks.
- Hourly rate limiting per recipient phone number.
- Safe template formatting (default fallback for missing keys).
- Configurable SMS splitting or truncation when messages exceed max_length.
- Consistent phone number masking.
"""
import datetime
import fnmatch
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("relay.router")


def mask_phone(phone: str) -> str:
    """Mask middle digits of phone number: 09121234567 -> 0912***4567."""
    phone = str(phone).strip()
    if len(phone) < 7:
        return "***"
    return f"{phone[:4]}***{phone[-4:]}"


class AlertRouter:
    def __init__(self, routing_config: Dict[str, Any], phone_numbers: Optional[List[str]] = None):
        self.config = routing_config
        raw_phones = phone_numbers if phone_numbers is not None else routing_config.get("phone_numbers", [])
        self.phone_numbers = [str(p).strip() for p in raw_phones if str(p).strip()]
        self.rules = routing_config.get("rules", [])
        self.default_recipients = routing_config.get("default_recipients", [])
        self.quiet_hours = routing_config.get("quiet_hours", {})
        self.dedup_window_minutes = int(routing_config.get("deduplication_window_minutes", 0))
        self.rate_limit_per_hour = int(routing_config.get("rate_limit_per_phone_per_hour", 0))

        # In-memory de-duplication state: key -> last_seen_epoch
        # key format: f"{alert_name}:{target}:{severity}:{status}"
        self._dedup_cache: Dict[str, float] = {}

        # In-memory rate limiting state: phone -> list of timestamp epochs
        self._rate_limit_cache: Dict[str, List[float]] = {}

    def _match_pattern(self, pattern: Optional[str], value: str) -> bool:
        if pattern is None or pattern == "" or pattern == "*":
            return True
        return fnmatch.fnmatch(value.lower(), pattern.lower())

    def _match_list_or_pattern(self, criteria: Any, value: str) -> bool:
        if criteria is None:
            return True
        if isinstance(criteria, list):
            return any(self._match_pattern(c, value) for c in criteria)
        return self._match_pattern(str(criteria), value)

    def is_quiet_hours(self, now: Optional[datetime.datetime] = None) -> bool:
        if not self.quiet_hours.get("enabled", False):
            return False

        if now is None:
            now = datetime.datetime.now()

        start_str = self.quiet_hours.get("start", "23:00")
        end_str = self.quiet_hours.get("end", "07:00")

        try:
            sh, sm = map(int, start_str.split(":"))
            eh, em = map(int, end_str.split(":"))
            start_t = datetime.time(sh, sm)
            end_t = datetime.time(eh, em)
            current_t = now.time()

            if start_t <= end_t:
                return start_t <= current_t <= end_t
            else:  # Spans across midnight
                return current_t >= start_t or current_t <= end_t
        except Exception as e:
            log.error("Failed to parse quiet hours: %s", e)
            return False

    def is_deduplicated(self, details: Dict[str, str], now_epoch: Optional[float] = None) -> bool:
        if self.dedup_window_minutes <= 0:
            return False

        if now_epoch is None:
            now_epoch = time.time()

        # Prune expired keys to avoid unbounded memory growth
        expiry_seconds = self.dedup_window_minutes * 60
        self._dedup_cache = {
            k: v for k, v in self._dedup_cache.items() if (now_epoch - v) < expiry_seconds
        }

        # Include status so "Cleared" / "Ended" isn't suppressed after "Raised"
        key = (
            f"{details.get('alert_name')}:"
            f"{details.get('target')}:"
            f"{details.get('severity')}:"
            f"{details.get('status')}"
        )
        last_time = self._dedup_cache.get(key)
        if last_time and (now_epoch - last_time) < expiry_seconds:
            return True

        return False

    def record_dedup_success(self, details: Dict[str, str], now_epoch: Optional[float] = None) -> None:
        """Mark alert as successfully processed for deduplication."""
        if self.dedup_window_minutes <= 0:
            return
        if now_epoch is None:
            now_epoch = time.time()
        key = (
            f"{details.get('alert_name')}:"
            f"{details.get('target')}:"
            f"{details.get('severity')}:"
            f"{details.get('status')}"
        )
        self._dedup_cache[key] = now_epoch

    def is_rate_limited(self, phone: str, now_epoch: Optional[float] = None) -> bool:
        if self.rate_limit_per_hour <= 0:
            return False

        if now_epoch is None:
            now_epoch = time.time()

        history = self._rate_limit_cache.get(phone, [])
        one_hour_ago = now_epoch - 3600
        valid_history = [t for t in history if t > one_hour_ago]

        if len(valid_history) >= self.rate_limit_per_hour:
            return True

        return False

    def record_send_for_rate_limit(self, phone: str, now_epoch: Optional[float] = None) -> None:
        """Record an actually sent message toward rate limit."""
        if self.rate_limit_per_hour <= 0:
            return
        if now_epoch is None:
            now_epoch = time.time()
        history = self._rate_limit_cache.get(phone, [])
        one_hour_ago = now_epoch - 3600
        valid_history = [t for t in history if t > one_hour_ago]
        valid_history.append(now_epoch)
        self._rate_limit_cache[phone] = valid_history

    def route_alert(self, details: Dict[str, str]) -> Tuple[List[Dict[str, str]], List[str]]:
        matched_recipients: Dict[str, Dict[str, str]] = {}
        matched_rules: List[str] = []

        # 1. Simple direct phone list (every alert dispatched to each number)
        if self.phone_numbers:
            matched_rules.append("all_configured_phones")
            for p in self.phone_numbers:
                if p not in matched_recipients:
                    matched_recipients[p] = {"name": "Admin", "phone": p, "bypass_quiet_hours": True}
            return list(matched_recipients.values()), matched_rules

        alert_name = details.get("alert_name", "")
        severity = details.get("severity", "")
        target = details.get("target", "")

        for rule in self.rules:
            rule_name = rule.get("name", "unnamed_rule")

            if not self._match_list_or_pattern(rule.get("severity"), severity):
                continue
            if not self._match_list_or_pattern(rule.get("alert_name"), alert_name):
                continue
            if not self._match_list_or_pattern(rule.get("target"), target):
                continue

            matched_rules.append(rule_name)
            for rec in rule.get("recipients", []):
                phone = rec.get("phone")
                if phone and phone not in matched_recipients:
                    matched_recipients[phone] = rec

        if not matched_recipients and self.default_recipients:
            matched_rules.append("default_recipients")
            for rec in self.default_recipients:
                phone = rec.get("phone")
                if phone and phone not in matched_recipients:
                    matched_recipients[phone] = rec

        return list(matched_recipients.values()), matched_rules


class SafeDict(dict):
    """Dictionary that returns empty string or placeholder for missing keys."""
    def __missing__(self, key):
        return f"{{{key}}}"


def format_sms_message(details: Dict[str, str], templates_config: Dict[str, Any]) -> List[str]:
    """Format SMS text using configured templates and handle character limits (split or truncate).
    
    Returns a list of message strings (single message or multiple if split_messages is enabled).
    """
    severity = details.get("severity", "").capitalize()
    template = templates_config.get(
        severity.lower(),
        templates_config.get("default", "Alert: {alert_name} on {target} [{severity}]. {message}")
    )

    safe_mapping = SafeDict({
        "alert_name": details.get("alert_name") or "Unknown",
        "severity": details.get("severity") or "Alert",
        "target": details.get("target") or "Unknown",
        "status": details.get("status") or "Raised",
        "message": details.get("message") or "",
        "url": details.get("url") or "",
        "timestamp": details.get("timestamp") or "",
        "alert_id": details.get("alert_id") or "",
    })

    try:
        text = template.format_map(safe_mapping)
    except Exception as e:
        log.error("Error formatting template '%s': %s. Using fallback.", template, e)
        text = f"Alert: {details.get('alert_name')} on {details.get('target')} [{details.get('severity')}]"

    max_len = int(templates_config.get("max_length", 480))
    split_messages = bool(templates_config.get("split_messages", False))

    if len(text) <= max_len:
        return [text]

    if not split_messages:
        return [text[:max_len - 3] + "..."]

    # Split into chunks of max_len with part headers e.g. (1/2)
    # Estimate parts
    parts: List[str] = []
    chunk_size = max_len - 8  # reserve room for (X/Y) suffix
    total_parts = (len(text) + chunk_size - 1) // chunk_size

    for idx in range(total_parts):
        part_content = text[idx * chunk_size : (idx + 1) * chunk_size]
        parts.append(f"{part_content} ({idx + 1}/{total_parts})")

    return parts
