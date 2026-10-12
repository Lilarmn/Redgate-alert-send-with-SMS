"""
SMS Gateway Client & Diagnostics Tool
======================================
بر اساس سند راهنمای رسمی API ارسال پیامک (نسخه ۱.۰)
Base URL: https://api.sms-gateway.example.com
Endpoint: POST /api/v1/sms/send
Headers: Content-Type: application/json, Accept-Language: fa|en, Authorization: Bearer {API_KEY}
Body: {"destination": "09121234567", "message": "متن پیامک"}
"""

import json
import os
import socket
import ssl
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import requests
import urllib3

# Suppress insecure request warnings if verify_ssl is disabled
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# توضیحات رسمی و خطایابی کدهای پاسخ API طبق مستندات
CODE_EXPLANATIONS = {
    "SENT_OK": {
        "fa": "پیام با موفقیت ارسال شد.",
        "en": "Message sent successfully.",
        "badge": "success",
        "hint": "درخواست توسط سرور پیامک پذیرفته شده و در صف ارسال قرار گرفت."
    },
    "INVALID_NUMBER": {
        "fa": "شماره مقصد نامعتبر است.",
        "en": "Invalid destination phone number.",
        "badge": "warning",
        "hint": "فرمت شماره باید ۱۱ رقمی و به صورت 09121234567 باشد."
    },
    "EMPTY_MESSAGE": {
        "fa": "پیامک خالی است.",
        "en": "SMS message cannot be empty.",
        "badge": "warning",
        "hint": "متن پیامک نباید خالی باشد (حداکثر ۴۸۰ کاراکتر)."
    },
    "UNAUTHORIZED": {
        "fa": "کلید دسترسی (API Key) نامعتبر است.",
        "en": "Unauthorized - Invalid API Key.",
        "badge": "danger",
        "hint": "مقدار API Key وارد شده معتبر نیست یا مجوز آن منقضی شده است. هدر Authorization را بررسی کنید."
    },
    "RATE_LIMIT_EXCEEDED": {
        "fa": "تعداد درخواست‌ها بیش از حد مجاز است.",
        "en": "Rate limit exceeded.",
        "badge": "warning",
        "hint": "تعداد درخواست‌های ارسالی در بازه زمانی مشخص بیش از سقف مجاز سرور بوده است."
    },
    "SERVER_ERROR": {
        "fa": "خطای داخلی سرور پیامک.",
        "en": "Internal SMS server error.",
        "badge": "danger",
        "hint": "سرور پیامک با خطای داخلی مواجه شده است. با پشتیبانی سرویس‌دهنده تماس بگیرید."
    },
    "SSL_ERROR": {
        "fa": "خطای تایید گواهی امنیتی SSL/TLS.",
        "en": "SSL/TLS Certificate verification failed.",
        "badge": "danger",
        "hint": "گواهی SSL سرور توسط سیستم شما معتبر شناخته نشد. در صورت استفاده از شبکه داخلی، می‌توانید گزینه تایید SSL را غیرفعال کنید."
    },
    "TIMEOUT_ERROR": {
        "fa": "زمان انتظار پاسخ (Timeout) به پایان رسید.",
        "en": "Connection timed out.",
        "badge": "danger",
        "hint": "سرور در زمان تعیین شده پاسخی ارسال نکرد. ارتباط اینترنتی، پورت یا فایروال را بررسی کنید."
    },
    "CONNECTION_ERROR": {
        "fa": "عدم امکان برقراری ارتباط با سرور.",
        "en": "Failed to establish network connection.",
        "badge": "danger",
        "hint": "ارتباط فیزیکی یا شبکه برقرار نشد. آدرس سرور، DNS یا فایروال سیستم را بررسی کنید."
    }
}


def load_config() -> Dict[str, Any]:
    """Load configuration from config.json with fallback defaults."""
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
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
                # Deep merge defaults
                for k, v in user_cfg.items():
                    if isinstance(v, dict) and k in default_config:
                        default_config[k].update(v)
                    else:
                        default_config[k] = v
        except Exception as e:
            print(f"[WARN] Error reading {CONFIG_PATH}: {e}")
    return default_config


def save_config(config: Dict[str, Any]) -> bool:
    """Save configuration to config.json."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[ERROR] Failed to save config: {e}")
        return False


def validate_iranian_phone(phone: str) -> Tuple[bool, str]:
    """Validate Iranian mobile phone number format."""
    clean = "".join(ch for ch in str(phone) if ch.isdigit())
    if clean.startswith("989") and len(clean) == 12:
        clean = "0" + clean[2:]
    elif clean.startswith("9") and len(clean) == 10:
        clean = "0" + clean

    if len(clean) == 11 and clean.startswith("09"):
        return True, clean
    return False, phone


class SmsClient:
    """SMS Gateway API Client with deep diagnostic capabilities."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or load_config()

    def reload(self):
        self.config = load_config()

    @property
    def server_cfg(self) -> Dict[str, Any]:
        return self.config.get("sms_server", {})

    @property
    def base_url(self) -> str:
        return self.server_cfg.get("base_url", "https://api.sms-gateway.example.com").rstrip("/")

    @property
    def send_endpoint(self) -> str:
        endpoint = self.server_cfg.get("send_endpoint", "/api/v1/sms/send")
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        return endpoint

    @property
    def full_send_url(self) -> str:
        return f"{self.base_url}{self.send_endpoint}"

    @property
    def api_key(self) -> str:
        return self.server_cfg.get("api_key", "").strip()

    @property
    def timeout(self) -> float:
        return float(self.server_cfg.get("timeout_seconds", 10))

    @property
    def verify_ssl(self) -> bool:
        return bool(self.server_cfg.get("verify_ssl", True))

    @property
    def accept_language(self) -> str:
        return self.server_cfg.get("accept_language", "fa")

    @property
    def mock_mode(self) -> bool:
        return bool(self.server_cfg.get("mock_mode", False))

    @property
    def mock_scenario(self) -> str:
        return self.server_cfg.get("mock_scenario", "SENT_OK")

    def _parse_host_and_port(self) -> Tuple[str, int, bool]:
        """Extract hostname, port and is_ssl from base_url."""
        parsed = urllib.parse.urlparse(self.base_url)
        hostname = parsed.hostname or "api.sms-gateway.example.com"
        is_ssl = parsed.scheme.lower() == "https"
        if parsed.port:
            port = parsed.port
        else:
            port = 443 if is_ssl else 80
        return hostname, port, is_ssl

    # -------------------------------------------------------------
    # Step-by-Step Network Diagnostics
    # -------------------------------------------------------------

    def diagnose_dns(self) -> Dict[str, Any]:
        """Step 1: Check DNS resolution for hostname."""
        hostname, _, _ = self._parse_host_and_port()
        start = time.perf_counter()
        try:
            addr_info = socket.getaddrinfo(hostname, None)
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            ips = sorted(list({info[4][0] for info in addr_info}))
            return {
                "step": "DNS Resolution",
                "title_fa": "تحلیل و تفکیک نام دامنه (DNS)",
                "target": hostname,
                "success": True,
                "ips": ips,
                "latency_ms": duration_ms,
                "message_fa": f"دامنه با موفقیت به آی‌پی ({', '.join(ips)}) تبدیل شد.",
                "details": f"Resolved in {duration_ms} ms to: {', '.join(ips)}"
            }
        except Exception as e:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            return {
                "step": "DNS Resolution",
                "title_fa": "تحلیل و تفکیک نام دامنه (DNS)",
                "target": hostname,
                "success": False,
                "ips": [],
                "latency_ms": duration_ms,
                "message_fa": f"خطا در تحلیل نام دامنه: {e}",
                "hint_fa": "بررسی کنید که آیا DNS سرور سیستم یا اینترنت در دسترس است و نام هاست صحیح وارد شده است.",
                "details": str(e)
            }

    def diagnose_tcp(self) -> Dict[str, Any]:
        """Step 2: Check raw TCP socket connection."""
        hostname, port, _ = self._parse_host_and_port()
        start = time.perf_counter()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((hostname, port))
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            sock.close()
            return {
                "step": "TCP Port Check",
                "title_fa": "بررسی ارتباط پورت شبکه (TCP Socket)",
                "target": f"{hostname}:{port}",
                "success": True,
                "latency_ms": duration_ms,
                "message_fa": f"اتصال به پورت {port} سرور با موفقیت برقرار شد.",
                "details": f"Connected to {hostname}:{port} in {duration_ms} ms"
            }
        except socket.timeout:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            return {
                "step": "TCP Port Check",
                "title_fa": "بررسی ارتباط پورت شبکه (TCP Socket)",
                "target": f"{hostname}:{port}",
                "success": False,
                "latency_ms": duration_ms,
                "message_fa": f"اتصال به پورت {port} به علت اتمام مهلت (Timeout) ناموفق بود.",
                "hint_fa": f"ممکن است فایروال ویندوز یا فایروال شبکه مانع خروج ترافیک به پورت {port} شود.",
                "details": "Connection timed out"
            }
        except Exception as e:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            return {
                "step": "TCP Port Check",
                "title_fa": "بررسی ارتباط پورت شبکه (TCP Socket)",
                "target": f"{hostname}:{port}",
                "success": False,
                "latency_ms": duration_ms,
                "message_fa": f"عدم امکان برقراری ارتباط سوکت با پورت {port}: {e}",
                "hint_fa": "پورت یا هاست مقصد مسدود است یا سرور خاموش است.",
                "details": str(e)
            }

    def diagnose_ssl(self) -> Dict[str, Any]:
        """Step 3: Check SSL/TLS Handshake and Certificate."""
        hostname, port, is_ssl = self._parse_host_and_port()
        if not is_ssl:
            return {
                "step": "SSL/TLS Handshake",
                "title_fa": "بررسی گواهینامه امنیتی SSL/TLS",
                "target": f"{hostname}:{port}",
                "success": True,
                "skipped": True,
                "message_fa": "پروتکل استفاده شده HTTP معمولی است و نیازی به گواهی SSL ندارد.",
                "details": "Plain HTTP used, SSL handshake skipped."
            }

        start = time.perf_counter()
        ctx = ssl.create_default_context()
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            with socket.create_connection((hostname, port), timeout=self.timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                    cert = ssock.getpeercert()
                    cipher = ssock.cipher()
                    tls_version = ssock.version()
                    duration_ms = round((time.perf_counter() - start) * 1000, 2)

                    cert_subject = dict(x[0] for x in cert.get("subject", [])) if cert else {}
                    common_name = cert_subject.get("commonName", "Unknown")
                    not_after = cert.get("notAfter", "Unknown") if cert else "Not verified"

                    return {
                        "step": "SSL/TLS Handshake",
                        "title_fa": "بررسی گواهینامه امنیتی SSL/TLS",
                        "target": hostname,
                        "success": True,
                        "latency_ms": duration_ms,
                        "tls_version": tls_version,
                        "cipher": cipher[0] if cipher else "Unknown",
                        "common_name": common_name,
                        "expires_at": not_after,
                        "message_fa": f"ارتباط امن TLS برقرار شد ({tls_version} - {common_name}).",
                        "details": f"TLS: {tls_version}, Cipher: {cipher}, Valid until: {not_after}"
                    }
        except ssl.SSLCertVerificationError as e:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            return {
                "step": "SSL/TLS Handshake",
                "title_fa": "بررسی گواهینامه امنیتی SSL/TLS",
                "target": hostname,
                "success": False,
                "latency_ms": duration_ms,
                "message_fa": "گواهی SSL سرور توسط ویندوز معتبر شناخته نشد (Self-signed یا منقضی شده).",
                "hint_fa": "می‌توانید در تب تنظیمات، گزینه «بررسی گواهی SSL (Verify SSL)» را خاموش کنید تا اتصال برقرار شود.",
                "details": str(e)
            }
        except Exception as e:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            return {
                "step": "SSL/TLS Handshake",
                "title_fa": "بررسی گواهینامه امنیتی SSL/TLS",
                "target": hostname,
                "success": False,
                "latency_ms": duration_ms,
                "message_fa": f"خطا در ایجاد نشست امن SSL/TLS: {e}",
                "details": str(e)
            }

    def diagnose_http_reachability(self) -> Dict[str, Any]:
        """Step 4: Check HTTP Server Reachability (HEAD/GET)."""
        start = time.perf_counter()
        try:
            # We ping the base_url with GET
            resp = requests.get(
                self.base_url,
                timeout=self.timeout,
                verify=self.verify_ssl,
                headers={"User-Agent": "SMS-Diagnostic-Tester/1.0"}
            )
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            server_header = resp.headers.get("Server", "Unknown")
            return {
                "step": "HTTP Server Ping",
                "title_fa": "بررسی دسترسی به وب‌سرور (HTTP Probe)",
                "target": self.base_url,
                "success": True,
                "status_code": resp.status_code,
                "latency_ms": duration_ms,
                "server": server_header,
                "message_fa": f"وب‌سرور با کد وضعیت {resp.status_code} پاسخ داد ({duration_ms} میلی‌ثانیه).",
                "details": f"Status: {resp.status_code}, Server: {server_header}"
            }
        except requests.exceptions.SSLError as e:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            return {
                "step": "HTTP Server Ping",
                "title_fa": "بررسی دسترسی به وب‌سرور (HTTP Probe)",
                "target": self.base_url,
                "success": False,
                "latency_ms": duration_ms,
                "message_fa": "خطای SSL در هنگام درخواست HTTP.",
                "hint_fa": "گزینه Verify SSL را در تنظیمات غیرفعال کنید.",
                "details": str(e)
            }
        except requests.exceptions.Timeout:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            return {
                "step": "HTTP Server Ping",
                "title_fa": "بررسی دسترسی به وب‌سرور (HTTP Probe)",
                "target": self.base_url,
                "success": False,
                "latency_ms": duration_ms,
                "message_fa": "عدم پاسخگویی وب‌سرور در مهلت مجاز (Timeout).",
                "details": "HTTP Request Timeout"
            }
        except Exception as e:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            return {
                "step": "HTTP Server Ping",
                "title_fa": "بررسی دسترسی به وب‌سرور (HTTP Probe)",
                "target": self.base_url,
                "success": False,
                "latency_ms": duration_ms,
                "message_fa": f"خطا در برقراری ارتباط HTTP: {e}",
                "details": str(e)
            }

    def run_full_diagnostics(self) -> Dict[str, Any]:
        """Run all diagnostic steps sequentially and return summarized health score."""
        self.reload()
        dns_res = self.diagnose_dns()
        tcp_res = self.diagnose_tcp() if dns_res["success"] else {
            "step": "TCP Port Check", "title_fa": "بررسی ارتباط پورت شبکه (TCP Socket)",
            "success": False, "skipped": True, "message_fa": "به دلیل خطای DNS بررسی نشد."
        }
        ssl_res = self.diagnose_ssl() if tcp_res.get("success") else {
            "step": "SSL/TLS Handshake", "title_fa": "بررسی گواهینامه امنیتی SSL/TLS",
            "success": False, "skipped": True, "message_fa": "به دلیل خطای TCP بررسی نشد."
        }
        http_res = self.diagnose_http_reachability() if tcp_res.get("success") else {
            "step": "HTTP Server Ping", "title_fa": "بررسی دسترسی به وب‌سرور (HTTP Probe)",
            "success": False, "skipped": True, "message_fa": "به دلیل خطای پورت بررسی نشد."
        }

        all_steps = [dns_res, tcp_res, ssl_res, http_res]
        passed_count = sum(1 for s in all_steps if s.get("success") and not s.get("skipped"))
        total_eval = len([s for s in all_steps if not s.get("skipped")])

        overall_healthy = (
            dns_res.get("success", False) and
            tcp_res.get("success", False) and
            (ssl_res.get("success", False) or ssl_res.get("skipped", False))
        )

        return {
            "timestamp": datetime.now().isoformat(),
            "overall_healthy": overall_healthy,
            "score": f"{passed_count}/{total_eval}",
            "hostname": self._parse_host_and_port()[0],
            "base_url": self.base_url,
            "send_url": self.full_send_url,
            "steps": all_steps
        }

    # -------------------------------------------------------------
    # Test SMS Dispatch (Real & Mock)
    # -------------------------------------------------------------

    def send_sms(self, destination: str, message: str) -> Dict[str, Any]:
        """
        Sends an SMS according to the official documentation:
        POST /api/v1/sms/send
        Headers:
          Content-Type: application/json
          Accept-Language: fa | en
          Authorization: Bearer {API_KEY}
        Body:
          { "destination": "09121234567", "message": "متن" }
        """
        self.reload()
        is_valid_phone, clean_phone = validate_iranian_phone(destination)

        # Build request inspection object
        url = self.full_send_url
        headers = {
            "Content-Type": "application/json",
            "Accept-Language": self.accept_language,
            "Authorization": f"Bearer {self.api_key}" if self.api_key else ""
        }
        body_dict = {
            "destination": clean_phone,
            "message": message
        }

        # Check for Mock Mode
        if self.mock_mode:
            return self._simulate_mock_send(clean_phone, message, url, headers, body_dict)

        # Real Execution
        start_time = time.perf_counter()
        request_log = {
            "url": url,
            "method": "POST",
            "headers": {
                "Content-Type": headers["Content-Type"],
                "Accept-Language": headers["Accept-Language"],
                "Authorization": f"Bearer {'*' * 8}...{self.api_key[-4:]}" if len(self.api_key) > 6 else (headers["Authorization"] or "NONE")
            },
            "body": body_dict
        }

        try:
            resp = requests.post(
                url,
                json=body_dict,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify_ssl
            )
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            raw_text = resp.text

            resp_json = None
            try:
                resp_json = resp.json()
            except Exception:
                resp_json = None

            # Parse standard response fields
            is_success = False
            code = "UNKNOWN"
            api_msg = ""
            resp_data = {}
            request_id = None
            timestamp = datetime.now().isoformat()

            if isinstance(resp_json, dict):
                is_success = bool(resp_json.get("success", False))
                code = resp_json.get("code", "SENT_OK" if (is_success and resp.status_code == 200) else "UNKNOWN")
                api_msg = resp_json.get("message", "")
                resp_data = resp_json.get("data", {})
                request_id = resp_json.get("requestId")
                timestamp = resp_json.get("timestamp", timestamp)
            else:
                is_success = (200 <= resp.status_code < 300)
                code = "SENT_OK" if is_success else f"HTTP_{resp.status_code}"
                api_msg = f"پاسخ غیر JSON از سرور: {raw_text[:200]}"

            explanation = CODE_EXPLANATIONS.get(code, {
                "fa": api_msg or f"کد وضعیت {code}",
                "en": code,
                "badge": "success" if is_success else "danger",
                "hint": "پاسخ از سرور پیامک دریافت شد."
            })

            return {
                "success": is_success,
                "status_code": resp.status_code,
                "elapsed_ms": elapsed_ms,
                "code": code,
                "message": api_msg,
                "explanation_fa": explanation["fa"],
                "hint_fa": explanation.get("hint", ""),
                "badge": explanation.get("badge", "info"),
                "requestId": request_id,
                "data": resp_data,
                "raw_response": raw_text,
                "json_response": resp_json,
                "request_info": request_log,
                "timestamp": timestamp,
                "mode": "real"
            }

        except requests.exceptions.SSLError as e:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return {
                "success": False,
                "status_code": None,
                "elapsed_ms": elapsed_ms,
                "code": "SSL_ERROR",
                "message": str(e),
                "explanation_fa": CODE_EXPLANATIONS["SSL_ERROR"]["fa"],
                "hint_fa": CODE_EXPLANATIONS["SSL_ERROR"]["hint"],
                "badge": "danger",
                "request_info": request_log,
                "raw_response": str(e),
                "timestamp": datetime.now().isoformat(),
                "mode": "real"
            }

        except requests.exceptions.Timeout as e:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return {
                "success": False,
                "status_code": None,
                "elapsed_ms": elapsed_ms,
                "code": "TIMEOUT_ERROR",
                "message": f"اتمام مهلت پاسخ ({self.timeout} ثانیه)",
                "explanation_fa": CODE_EXPLANATIONS["TIMEOUT_ERROR"]["fa"],
                "hint_fa": CODE_EXPLANATIONS["TIMEOUT_ERROR"]["hint"],
                "badge": "danger",
                "request_info": request_log,
                "raw_response": str(e),
                "timestamp": datetime.now().isoformat(),
                "mode": "real"
            }

        except requests.exceptions.ConnectionError as e:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return {
                "success": False,
                "status_code": None,
                "elapsed_ms": elapsed_ms,
                "code": "CONNECTION_ERROR",
                "message": f"عدم امکان اتصال به {url}: {e}",
                "explanation_fa": CODE_EXPLANATIONS["CONNECTION_ERROR"]["fa"],
                "hint_fa": CODE_EXPLANATIONS["CONNECTION_ERROR"]["hint"],
                "badge": "danger",
                "request_info": request_log,
                "raw_response": str(e),
                "timestamp": datetime.now().isoformat(),
                "mode": "real"
            }

        except Exception as e:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return {
                "success": False,
                "status_code": None,
                "elapsed_ms": elapsed_ms,
                "code": "ERROR",
                "message": str(e),
                "explanation_fa": f"خطای پیش‌بینی نشده: {e}",
                "hint_fa": "لاگ خطای برنامه را بررسی نمایید.",
                "badge": "danger",
                "request_info": request_log,
                "raw_response": str(e),
                "timestamp": datetime.now().isoformat(),
                "mode": "real"
            }

    def _simulate_mock_send(
        self, phone: str, message: str, url: str, headers: dict, body: dict
    ) -> Dict[str, Any]:
        """Simulates response strictly adhering to SMS Gateway API documentation specification."""
        scenario = self.mock_scenario
        now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        req_id = f"mock-req-{int(time.time())}"
        masked_auth = f"Bearer {'*' * 8}...{self.api_key[-4:]}" if len(self.api_key) > 6 else headers.get("Authorization", "")

        request_log = {
            "url": url,
            "method": "POST",
            "headers": {
                "Content-Type": headers["Content-Type"],
                "Accept-Language": headers["Accept-Language"],
                "Authorization": masked_auth
            },
            "body": body
        }

        # Validate conditions if scenario is SENT_OK but phone or message is invalid
        if scenario == "SENT_OK":
            if not phone or len(phone) < 10:
                scenario = "INVALID_NUMBER"
            elif not message.strip():
                scenario = "EMPTY_MESSAGE"

        if scenario == "SENT_OK":
            mock_body = {
                "success": True,
                "code": "SENT_OK",
                "message": "پیام با موفقیت ارسال شد.",
                "httpStatus": 200,
                "data": {
                    "messages": [
                        {
                            "id": f"msg{int(time.time())}",
                            "destination": phone,
                            "status": "sent",
                            "sentAt": now_iso
                        }
                    ],
                    "count": 1
                },
                "timestamp": now_iso,
                "requestId": req_id
            }
            return {
                "success": True,
                "status_code": 200,
                "elapsed_ms": 42.5,
                "code": "SENT_OK",
                "message": "پیام با موفقیت ارسال شد. (حالت شبیه‌ساز)",
                "explanation_fa": CODE_EXPLANATIONS["SENT_OK"]["fa"],
                "hint_fa": "شبیه‌سازی موفق بر اساس داکیومنت رسمی درگاه پیامک.",
                "badge": "success",
                "requestId": req_id,
                "data": mock_body["data"],
                "raw_response": json.dumps(mock_body, indent=2, ensure_ascii=False),
                "json_response": mock_body,
                "request_info": request_log,
                "timestamp": now_iso,
                "mode": "mock"
            }

        elif scenario == "INVALID_NUMBER":
            mock_body = {
                "success": False,
                "code": "INVALID_NUMBER",
                "message": "شماره مقصد نامعتبر است.",
                "httpStatus": 400,
                "data": {},
                "timestamp": now_iso,
                "requestId": req_id
            }
            return {
                "success": False,
                "status_code": 400,
                "elapsed_ms": 35.0,
                "code": "INVALID_NUMBER",
                "message": "شماره مقصد نامعتبر است.",
                "explanation_fa": CODE_EXPLANATIONS["INVALID_NUMBER"]["fa"],
                "hint_fa": CODE_EXPLANATIONS["INVALID_NUMBER"]["hint"],
                "badge": "warning",
                "requestId": req_id,
                "data": {},
                "raw_response": json.dumps(mock_body, indent=2, ensure_ascii=False),
                "json_response": mock_body,
                "request_info": request_log,
                "timestamp": now_iso,
                "mode": "mock"
            }

        elif scenario == "EMPTY_MESSAGE":
            mock_body = {
                "success": False,
                "code": "EMPTY_MESSAGE",
                "message": "پیامک خالی است.",
                "httpStatus": 400,
                "data": {},
                "timestamp": now_iso,
                "requestId": req_id
            }
            return {
                "success": False,
                "status_code": 400,
                "elapsed_ms": 28.0,
                "code": "EMPTY_MESSAGE",
                "message": "پیامک خالی است.",
                "explanation_fa": CODE_EXPLANATIONS["EMPTY_MESSAGE"]["fa"],
                "hint_fa": CODE_EXPLANATIONS["EMPTY_MESSAGE"]["hint"],
                "badge": "warning",
                "requestId": req_id,
                "data": {},
                "raw_response": json.dumps(mock_body, indent=2, ensure_ascii=False),
                "json_response": mock_body,
                "request_info": request_log,
                "timestamp": now_iso,
                "mode": "mock"
            }

        elif scenario == "UNAUTHORIZED":
            mock_body = {
                "success": False,
                "code": "UNAUTHORIZED",
                "message": "کلید دسترسی معتبر نیست.",
                "httpStatus": 401,
                "data": {},
                "timestamp": now_iso,
                "requestId": req_id
            }
            return {
                "success": False,
                "status_code": 401,
                "elapsed_ms": 30.0,
                "code": "UNAUTHORIZED",
                "message": "کلید دسترسی معتبر نیست.",
                "explanation_fa": CODE_EXPLANATIONS["UNAUTHORIZED"]["fa"],
                "hint_fa": CODE_EXPLANATIONS["UNAUTHORIZED"]["hint"],
                "badge": "danger",
                "requestId": req_id,
                "data": {},
                "raw_response": json.dumps(mock_body, indent=2, ensure_ascii=False),
                "json_response": mock_body,
                "request_info": request_log,
                "timestamp": now_iso,
                "mode": "mock"
            }

        elif scenario == "RATE_LIMIT_EXCEEDED":
            mock_body = {
                "success": False,
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "تعداد درخواست‌ها بیش از حد مجاز است.",
                "httpStatus": 429,
                "data": {},
                "timestamp": now_iso,
                "requestId": req_id
            }
            return {
                "success": False,
                "status_code": 429,
                "elapsed_ms": 25.0,
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "تعداد درخواست‌ها بیش از حد مجاز است.",
                "explanation_fa": CODE_EXPLANATIONS["RATE_LIMIT_EXCEEDED"]["fa"],
                "hint_fa": CODE_EXPLANATIONS["RATE_LIMIT_EXCEEDED"]["hint"],
                "badge": "warning",
                "requestId": req_id,
                "data": {},
                "raw_response": json.dumps(mock_body, indent=2, ensure_ascii=False),
                "json_response": mock_body,
                "request_info": request_log,
                "timestamp": now_iso,
                "mode": "mock"
            }

        else:  # SERVER_ERROR
            mock_body = {
                "success": False,
                "code": "SERVER_ERROR",
                "message": "خطای داخلی سرور پیامک.",
                "httpStatus": 500,
                "data": {},
                "timestamp": now_iso,
                "requestId": req_id
            }
            return {
                "success": False,
                "status_code": 500,
                "elapsed_ms": 60.0,
                "code": "SERVER_ERROR",
                "message": "خطای داخلی سرور پیامک.",
                "explanation_fa": CODE_EXPLANATIONS["SERVER_ERROR"]["fa"],
                "hint_fa": CODE_EXPLANATIONS["SERVER_ERROR"]["hint"],
                "badge": "danger",
                "requestId": req_id,
                "data": {},
                "raw_response": json.dumps(mock_body, indent=2, ensure_ascii=False),
                "json_response": mock_body,
                "request_info": request_log,
                "timestamp": now_iso,
                "mode": "mock"
            }
