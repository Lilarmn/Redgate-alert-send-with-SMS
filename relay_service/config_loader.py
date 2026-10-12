"""Robust Configuration loader and validator.

Features:
- Character-by-character comment stripper supporting // and /* */ comments
  strictly outside quoted strings, properly respecting escaped backslashes (e.g. "C:\\data\\")
  and asterisks inside strings (e.g. "SQL/*PROD*").
- Validates all configuration sections, routing rules, SMS provider configurations,
  and template placeholders at startup with human-readable error messages.
"""
import json
import os
import re
from typing import Any, Dict, Set


def _strip_comments(text: str) -> str:
    """Strip // and /* */ comments strictly outside of JSON string literals.
    
    Handles Windows path backslashes (e.g. 'C:\\data\\') and comments inside strings properly.
    """
    out = []
    i = 0
    n = len(text)
    in_string = False

    while i < n:
        ch = text[i]

        if in_string:
            out.append(ch)
            if ch == '\\':
                # Escaped character inside string literal; consume next char verbatim
                if i + 1 < n:
                    i += 1
                    out.append(text[i])
            elif ch == '"':
                in_string = False
            i += 1
            continue

        # Outside string
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        # Check for multi-line comment /* ... */
        if ch == '/' and i + 1 < n and text[i + 1] == '*':
            i += 2
            while i + 1 < n and not (text[i] == '*' and text[i + 1] == '/'):
                if text[i] == '\n':
                    out.append('\n')  # preserve line count
                i += 1
            i += 2  # skip closing */
            continue

        # Check for single-line comment // ...
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            i += 2
            while i < n and text[i] != '\n':
                i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def load_config(file_path: str) -> Dict[str, Any]:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Configuration file not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        raw_content = f.read()

    clean_content = _strip_comments(raw_content)
    try:
        config = json.loads(clean_content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON syntax in config file ({file_path}): {e}") from e

    validate_config(config)
    return config


def save_config(config: Dict[str, Any], file_path: str) -> None:
    """Save configuration dictionary to JSON file cleanly."""
    validate_config(config)
    temp_path = file_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    # Atomic replace on Windows
    if os.path.exists(file_path):
        os.replace(temp_path, file_path)
    else:
        os.rename(temp_path, file_path)


VALID_TEMPLATE_VARS = {
    "alert_id", "alert_name", "severity", "target", "status", "message", "url", "timestamp"
}


def _validate_template_string(name: str, tmpl: str) -> None:
    placeholders = re.findall(r"\{([a-zA-Z0-9_]+)\}", tmpl)
    for p in placeholders:
        if p not in VALID_TEMPLATE_VARS:
            raise ValueError(
                f"Invalid placeholder '{{{p}}}' in templates.{name}. "
                f"Valid placeholders are: {sorted(VALID_TEMPLATE_VARS)}"
            )


def validate_config(cfg: Dict[str, Any]) -> None:
    if not isinstance(cfg, dict):
        raise ValueError("Configuration root must be a JSON object/dictionary.")

    # 1. Server settings
    server = cfg.get("server")
    if not isinstance(server, dict):
        raise ValueError("Missing or invalid 'server' block in config.")
    if "port" not in server or not isinstance(server["port"], int):
        raise ValueError("'server.port' must be an integer (e.g. 8000).")
    if "host" not in server or not isinstance(server["host"], str):
        raise ValueError("'server.host' must be a string (e.g. '0.0.0.0').")

    # 2. Storage
    storage = cfg.get("storage")
    if not isinstance(storage, dict):
        raise ValueError("Missing or invalid 'storage' block in config.")
    if "raw_payloads_dir" not in storage or not isinstance(storage["raw_payloads_dir"], str):
        raise ValueError("'storage.raw_payloads_dir' must be a valid directory path string.")
    if "database_path" not in storage or not isinstance(storage["database_path"], str):
        raise ValueError("'storage.database_path' must be a valid SQLite file path string.")

    # 3. Field mappings
    field_mappings = cfg.get("field_mappings")
    if not isinstance(field_mappings, dict):
        raise ValueError("Missing or invalid 'field_mappings' block in config.")
    for req_field in ["alert_id", "alert_name", "severity", "target", "message"]:
        if req_field not in field_mappings or not isinstance(field_mappings[req_field], list):
            raise ValueError(f"'field_mappings.{req_field}' must be a list of candidate keys.")

    # 4. Phone numbers & Routing
    phone_numbers = cfg.get("phone_numbers")
    if phone_numbers is not None and not isinstance(phone_numbers, list):
        raise ValueError("'phone_numbers' must be a list of phone numbers.")

    routing = cfg.get("routing")
    if routing is None:
        cfg["routing"] = {"rules": [], "default_recipients": []}
        routing = cfg["routing"]
    elif not isinstance(routing, dict):
        raise ValueError("Missing or invalid 'routing' block in config.")

    rules = routing.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("'routing.rules' must be a list.")
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"Routing rule #{i+1} must be an object.")
        if "recipients" not in rule or not isinstance(rule["recipients"], list) or not rule["recipients"]:
            raise ValueError(f"Routing rule #{i+1} must specify a non-empty 'recipients' list.")
        for j, rec in enumerate(rule["recipients"]):
            if not isinstance(rec, dict) or "phone" not in rec or not str(rec["phone"]).strip():
                raise ValueError(f"Routing rule #{i+1} recipient #{j+1} must specify a valid 'phone'.")

    # 5. Templates
    templates = cfg.get("templates")
    if not isinstance(templates, dict):
        raise ValueError("Missing or invalid 'templates' block in config.")
    for key, val in templates.items():
        if key == "max_length":
            if not isinstance(val, int) or val <= 0:
                raise ValueError("'templates.max_length' must be a positive integer.")
        elif key == "split_messages":
            if not isinstance(val, bool):
                raise ValueError("'templates.split_messages' must be a boolean.")
        elif isinstance(val, str):
            _validate_template_string(key, val)

    # 6. Provider
    sms = cfg.get("sms")
    if not isinstance(sms, dict):
        raise ValueError("Missing or invalid 'sms' block in config.")
    provider = sms.get("provider")
    if provider not in ("mock", "http"):
        raise ValueError(f"'sms.provider' must be either 'mock' or 'http' (got: {provider}).")
    if provider == "http":
        http_cfg = sms.get("http")
        if not isinstance(http_cfg, dict):
            raise ValueError("'sms.http' block is required when provider is 'http'.")
        if not http_cfg.get("url"):
            raise ValueError("'sms.http.url' is required.")

    dispatch_mode = sms.get("dispatch_mode", "auto")
    if dispatch_mode not in ("auto", "manual"):
        raise ValueError(f"'sms.dispatch_mode' must be either 'auto' or 'manual' (got: {dispatch_mode}).")
