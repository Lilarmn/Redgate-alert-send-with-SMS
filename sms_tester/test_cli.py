"""
SMS Tester - Command Line Interface (CLI)
=========================================
ابزار ترمینالی سریع جهت بررسی اتصال، عیب‌یابی شبکه و تست ارسال پیامک
بر اساس مستندات رسمی سرویس پیامک (نسخه ۱.۰)

دستورات قابل استفاده:
  python test_cli.py --diagnose
  python test_cli.py --send 09121234567 -m "متن تست"
  python test_cli.py --info
  python test_cli.py --mock on|off
"""

import argparse
import json
import sys

# Ensure UTF-8 stdout on Windows console
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from sms_client import SmsClient, load_config, save_config


def print_banner():
    print("=" * 68)
    print("  📡 SMS Gateway API Tester & Diagnostic Utility (v1.0)")
    print("  سند راهنمای رسمی API ارسال پیامک - نسخه ۱.۰")
    print("=" * 68)


def cmd_diagnose():
    print_banner()
    print("\n🔍 در حال بررسی ۴ مرحله‌ای زیرساخت و شبکه ارتباطی...")
    print("-" * 68)

    client = SmsClient()
    res = client.run_full_diagnostics()

    steps = res.get("steps", [])
    for idx, s in enumerate(steps, 1):
        status_symbol = "✅ [PASS]" if s.get("success") else ("⚠️ [SKIP]" if s.get("skipped") else "❌ [FAIL]")
        print(f"\nمرحله {idx}: {s.get('title_fa')} ({s.get('step')})")
        print(f"  وضعیت:   {status_symbol}")
        print(f"  هدف:     {s.get('target', 'N/A')}")
        if s.get("latency_ms"):
            print(f"  زمان:    {s.get('latency_ms')} میلی‌ثانیه")
        print(f"  پیام:    {s.get('message_fa')}")
        if s.get("hint_fa"):
            print(f"  💡 راهنما: {s.get('hint_fa')}")
        if s.get("details"):
            print(f"  جزئیات:  {s.get('details')}")

    print("\n" + "=" * 68)
    if res.get("overall_healthy"):
        print(f"🎉 نتیجه کلی: شبکه کاملاً سالم و آماده اتصال است (امتیاز: {res.get('score')})")
    else:
        print(f"⚠️ نتیجه کلی: موانعی در اتصال به سرور پیامک شناسایی شد (امتیاز: {res.get('score')})")
    print("=" * 68)


def cmd_send(phone: str, message: str):
    print_banner()
    client = SmsClient()
    print(f"\n🚀 در حال ارسال پیامک به شماره: {phone}")
    print(f"📝 متن پیامک: {message}")
    print(f"🎯 آدرس سرور: {client.full_send_url}")
    print(f"🧪 حالت: {'شبیه‌ساز (Mock Mode)' if client.mock_mode else 'سرور واقعی (Live Server)'}")
    print("-" * 68)

    res = client.send_sms(phone, message)

    status_symbol = "✅ موفق" if res.get("success") else "❌ ناموفق"
    print(f"\nنتیجه ارسال:      {status_symbol}")
    print(f"کد منطقی (code):   {res.get('code')}")
    print(f"کد وضعیت HTTP:     {res.get('status_code') or 'None'}")
    print(f"زمان پاسخ:        {res.get('elapsed_ms')} میلی‌ثانیه")
    print(f"شناسه پیگیری:     {res.get('requestId') or 'ندارد'}")
    print(f"توضیح فارسی:      {res.get('explanation_fa')}")
    if res.get("hint_fa"):
        print(f"💡 اقدام پیشنهادی: {res.get('hint_fa')}")

    print("\n📦 پاسخ خام سرور (Raw Response):")
    print(res.get("raw_response", "پاسخی دریافت نشد"))
    print("=" * 68)


def cmd_info():
    print_banner()
    cfg = load_config()
    srv = cfg.get("sms_server", {})
    def_cfg = cfg.get("defaults", {})

    print("\n⚙️ مشخصات و کانفیگ فعلی سیستم:")
    print(f"  Base URL:        {srv.get('base_url')}")
    print(f"  Endpoint:        {srv.get('send_endpoint')}")
    print(f"  Full URL:        {srv.get('base_url', '').rstrip('/')}{srv.get('send_endpoint')}")
    masked_key = f"{'*' * 8}...{srv.get('api_key', '')[-4:]}" if len(srv.get('api_key', '')) > 6 else (srv.get('api_key') or "تعریف نشده")
    print(f"  API Key:         {masked_key}")
    print(f"  Accept-Language: {srv.get('accept_language')}")
    print(f"  Timeout:         {srv.get('timeout_seconds')} ثانیه")
    print(f"  Verify SSL:      {srv.get('verify_ssl')}")
    print(f"  Mock Mode:       {'فعال (Simulation)' if srv.get('mock_mode') else 'غیرفعال (Real Server)'}")
    print(f"  Mock Scenario:   {srv.get('mock_scenario')}")
    print(f"  Default Phone:   {def_cfg.get('phone')}")
    print("=" * 68)


def cmd_mock(action: str):
    cfg = load_config()
    if action.lower() in ("on", "1", "true", "yes"):
        cfg["sms_server"]["mock_mode"] = True
        save_config(cfg)
        print("✅ حالت شبیه‌ساز (Mock Mode) فعال شد.")
    elif action.lower() in ("off", "0", "false", "no"):
        cfg["sms_server"]["mock_mode"] = False
        save_config(cfg)
        print("✅ حالت ارسال واقعی (Real Server) فعال شد.")
    else:
        print("استفاده: python test_cli.py --mock on|off")


def main():
    parser = argparse.ArgumentParser(description="SMS Gateway Server Diagnostic & Test CLI")
    parser.add_argument("--diagnose", "-d", action="store_true", help="اجرای بررسی کامل عیب‌یابی شبکه و سرور")
    parser.add_argument("--send", "-s", metavar="PHONE", type=str, help="ارسال پیامک آزمایشی به شماره مشخص")
    parser.add_argument("--message", "-m", type=str, default="تست اتصال به سامانه پیامک", help="متن پیامک ارسالی")
    parser.add_argument("--info", "-i", action="store_true", help="نمایش کانفیگ فعلی و آدرس‌ها")
    parser.add_argument("--mock", choices=["on", "off"], help="فعال یا غیرفعال‌سازی شبیه‌ساز آفلاین")

    args = parser.parse_args()

    if args.diagnose:
        cmd_diagnose()
    elif args.send:
        cmd_send(args.send, args.message)
    elif args.info:
        cmd_info()
    elif args.mock:
        cmd_mock(args.mock)
    else:
        # Default behavior if run without args: show info and instructions
        cmd_info()
        print("\nراهنمای استفاده سریع:")
        print("  1. تست سلامت زیرساخت:       python test_cli.py --diagnose")
        print("  2. ارسال پیامک آزمایشی:     python test_cli.py --send 09121234567 -m \"متن تست\"")
        print("  3. اجرای داشبورد تحت وب:    run.bat  یا  python app.py")


if __name__ == "__main__":
    main()
