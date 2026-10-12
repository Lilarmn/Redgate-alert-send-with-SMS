"""Test sender script simulating Redgate Monitor webhook POST requests.

Usage:
  python post_test_alert.py [endpoint_url] [payload_file]
Default endpoint: http://127.0.0.1:5000/webhook/redgate
"""
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8000/webhook/redgate"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PAYLOAD_FILE = os.path.join(BASE_DIR, "sample_redgate_payload.json")


def post_alert(url: str, payload_data: bytes, headers: dict = None) -> None:
    if headers is None:
        headers = {"Content-Type": "application/json; charset=utf-8"}

    req = urllib.request.Request(url, data=payload_data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp_body = resp.read().decode("utf-8")
            print(f"[{resp.status} {resp.reason}] Response:\n{resp_body}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        print(f"[HTTP Error {e.code}]: {err_body}")
    except urllib.error.URLError as e:
        print(f"[Connection Error]: {e.reason}")


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    payload_file = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PAYLOAD_FILE

    print(f"Posting alert to: {url}")
    print(f"Reading payload from: {payload_file}")

    if not os.path.exists(payload_file):
        print(f"File not found: {payload_file}")
        sys.exit(1)

    with open(payload_file, "rb") as f:
        data = f.read()

    post_alert(url, data)


if __name__ == "__main__":
    main()
