# Redgate SQL Monitor Webhook to SMS Alert System

A production-grade, lightweight, and resilient relay system that connects **Redgate SQL Monitor** alerts directly to corporate SMS Gateways. Designed for high-availability database administration (DBA) environments with full delivery auditing in **Microsoft SQL Server** / SQLite and an interactive visual web dashboard.

---

## 🌟 Key Architecture & Highlights

```
 [Redgate SQL Monitor]
        │ Webhook POST (JSON)
        ▼
 [Webhook Listener & Relay Server] (Port 8000)
        │
        ├──► 💾 [Database Audit Store] (SQL Server: redgate_alerts & sms_delivery_logs)
        │
        ├──► 📱 [Rule-Based Router & Dispatcher] (Severity, Target, Deduplication)
        │         │
        │         ▼
        │    [Corporate SMS Gateway] (REST API / Bearer Auth)
        │         │
        │         ▼
        │    [DBA / SysAdmin On-Call Mobile Devices]
        │
        └──► 📊 [Live Visual Management Dashboard] (http://localhost:8000)
```

### Core Features
- **Redgate SQL Monitor Integration**: Standard webhook receiver for alerts (Deadlocks, Job Failures, High CPU, Disk Space, etc.).
- **Smart Alert Routing & Deduplication**: Route critical alerts to on-call DBAs, filter by severity, and suppress duplicate notifications within a sliding time window.
- **Visual Web Management Panel (Port 8000)**: Real-time table of received webhooks, SMS delivery logs, dynamic phone number management, and template editors.
- **Enterprise SQL Server Storage**: Automatically logs all incoming payloads and SMS dispatches into Microsoft SQL Server tables with automatic table creation.
- **Standalone SMS Diagnostics Suite (`sms_tester`)**: Independent 4-step deep network diagnostic utility (DNS resolution, TCP socket reachability, TLS handshake, and HTTP probe) with web UI on port 8080.
- **Offline Resilient Deployment**: Includes offline Python package wheels (`wheelhouse/`) for air-gapped database servers.

---

## 📁 Repository Structure

```
.
├── relay_service/              # Core Webhook Listener & Alert Relay Service
│   ├── server.py               # Flask + Waitress HTTP production server
│   ├── dispatcher.py           # SMS dispatch engine and retry loop
│   ├── router.py               # Alert parsing, routing rules, deduplication
│   ├── db_manager.py           # SQL Server (pyodbc) and SQLite persistence
│   ├── config.json             # Service configuration & credentials
│   ├── templates/index.html    # Real-time visual dashboard
│   ├── adapters/               # SMS provider adapters (HTTP REST, Mock)
│   ├── tests/                  # Automated unit and integration tests
│   └── wheelhouse/             # Pinned offline Python wheel packages
│
├── sms_tester/                 # Standalone SMS Gateway Diagnostic Tool & Tester
│   ├── app.py                  # Standalone diagnostic web application (Port 8080)
│   ├── sms_client.py           # Core diagnostic client (DNS, TCP, SSL, HTTP)
│   ├── test_cli.py             # Command-line interface for terminal testing
│   ├── config.json             # Gateway connection settings
│   └── templates/index.html    # Interactive diagnostics dashboard
│
├── run.bat                     # Single-click launcher for Windows
└── .gitignore                  # Git ignore rules for logs, databases, and cache
```

---

## 🚀 Quick Start

### 1. Prerequisites
- Python 3.10+ (tested on Python 3.10 – 3.14 on Windows Server).
- Optional: Microsoft SQL Server 2016+ (or local SQLite for lightweight setups).

### 2. Running the Relay Service

Double-click `run.bat` or run from terminal:
```bash
cd relay_service
python server.py config.json
```
The server will start on `http://localhost:8000`.

### 3. Running the SMS Diagnostic Tool (`sms_tester`)

To diagnose network connectivity and test SMS gateway credentials:
```bash
cd sms_tester
python app.py
```
Open `http://127.0.0.1:8080` in your browser. Alternatively, run via CLI:
```bash
python test_cli.py --diagnose
python test_cli.py --send 09120000000 -m "Test alert message"
```

---

## ⚙️ Configuration (`relay_service/config.json`)

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 8000
  },
  "database": {
    "enabled": true,
    "type": "mssql",
    "server": ".\\SQLEXPRESS",
    "database": "AlertWebhook",
    "auth_type": "windows"
  },
  "sms": {
    "provider": "http",
    "http": {
      "url": "https://api.sms-gateway.example.com/v1/sms/send",
      "auth_type": "bearer",
      "api_key": "YOUR_SMS_API_KEY_HERE"
    }
  },
  "phone_numbers": [
    "09120000000"
  ]
}
```

---

## 🧪 Running Automated Tests

Run the test suite for both modules:

```bash
# Test Relay Service
python -m unittest discover -s relay_service/tests -p "test_*.py"

# Test SMS Tester Diagnostic Suite
python -m unittest discover -s sms_tester -p "test_*.py"
```

---

## 📄 License

MIT License. Designed for secure enterprise DBA environments.
