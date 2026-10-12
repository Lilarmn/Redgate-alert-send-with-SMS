# Redgate Alert → SMS Relay Service & Visual Management Panel

A transparent, lightweight, production-grade relay service that connects **Redgate SQL Monitor** alerts directly to your corporate SMS Gateway and stores all alerts and delivery audit records inside **Microsoft SQL Server** (or local SQLite).

Includes an interactive **Visual Web Dashboard on Port 8000** (`http://localhost:8000`) so you have 100% visibility into what happens behind the scenes, with zero black-box mystery.

---

## 🌟 1. System Flow & Architecture (No Black Box)

```
 [1. Redgate SQL Monitor]
         │ Webhook POST (JSON)
         ▼
 [2. Webhook Listener on Port 8000] (http://<server-ip>:8000/webhook/redgate)
         │
         ├──► 💾 [3. Save to SQL Server Database] Table: redgate_alerts
         │
         ├──► 📱 [4. Format SMS & Dispatch to All Configured Phone Numbers]
         │         (e.g., 09121234567, 09129876543, ...)
         │         │
         │         ▼
         │    [5. Corporate SMS Gateway Server] (HTTP REST / Provider API)
         │         │
         │         ▼
         │    [6. Mobile Phones Receive SMS Alert]
         │
         └──► 📝 [7. Record Delivery Audit] Table: sms_delivery_logs
                   & Live Visual Monitor Feed
```

---

## 🚀 2. How to Start the Service (Single Click)

You don't have to guess if the service is running in the background.

### Option A: Interactive Console (Recommended)
Simply double-click:
```
run.bat
```
*(Located in `c:\Users\Arman\Desktop\SMS\run.bat` or inside `relay_service\run.bat`)*

1. A terminal window opens showing real-time event logs.
2. Your default web browser opens automatically to:
   **`http://localhost:8000`**
3. You immediately see the live monitor, current statistics, and settings!
4. To stop the service, press `Ctrl+C` in the terminal window.

---

### Option B: Automatic Windows Service
If you want the service to start automatically on Windows boot:
1. Open PowerShell as **Administrator**.
2. Run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install_windows_service.ps1
   ```
Even as a background Windows Service, **the Visual Dashboard remains 100% accessible at `http://localhost:8000`** in your browser at all times!

---

## 🌐 3. Visual Dashboard Features (`http://localhost:8000`)

### 📊 1. Live Monitor Tab
- **Incoming Webhooks Table**: See every webhook received from Redgate (timestamp, alert name, severity, target SQL Server, status, source IP). Click **"View JSON"** to view the exact payload received.
- **SMS Delivery Logs Table**: View every SMS dispatched, recipient phone numbers, message texts, delivery status (SENT / FAILED), and SMS provider response codes.
- **Auto-Refresh**: Automatically refreshes every 3 seconds so you can watch alerts arrive live.

### ⚙️ 2. Visual Settings Tab (No JSON Editing Required)
- **📱 Phone Numbers**:
  - Add or remove phone numbers with a single click.
  - Every incoming alert is formatted and dispatched to **all phone numbers** in this list.
- **🌐 SMS Gateway Configuration**:
  - Switch between **Real SMS Server (HTTP)** and **Simulation (Mock)**.
  - Set Gateway URL, Auth Token / API Key, Header Type, and Field Names.
- **🗄️ SQL Server Database Configuration**:
  - Configure Host/Server (e.g., `localhost` or `SERVER\SQLEXPRESS`), Port (`1433`), Database name, and credentials.
  - Click **🔌 Test Connection** to verify database connectivity instantly.
  - Click **🛠️ Create / Verify Tables** to automatically generate `redgate_alerts` and `sms_delivery_logs` tables in SQL Server!
- **📝 SMS Message Templates**:
  - Customize SMS texts for High, Medium, and Low severity alerts.
  - Available placeholders: `{alert_name}`, `{target}`, `{severity}`, `{status}`, `{message}`, `{timestamp}`.
- Click **💾 Save All Settings** to apply changes immediately without stopping the server.

### 🧪 3. Testing Tools Tab
- **Send Test SMS**: Enter any phone number, write a custom message, and click **"Send Test SMS Now"** to verify your SMS provider.
- **Simulate Redgate Webhook**: Choose pre-configured sample alerts (Deadlock, High CPU, Low Disk Space, SQL Job Failure) and fire them into the webhook to test the entire end-to-end pipeline.

### 📜 4. Live Logs Tab
- Terminal console stream viewing `logs/relay.log` in real time with auto-scroll.

---

## 🗄️ 4. SQL Server Database Tables

When SQL Server is enabled in the Visual Settings, the following tables are automatically maintained:

### 1. `redgate_alerts` (Incoming Alert Store)
| Column | Type | Description |
|---|---|---|
| `id` | INT IDENTITY | Primary Key |
| `created_at` | DATETIME2 | Timestamp received |
| `alert_id` | NVARCHAR(100) | Redgate alert ID |
| `alert_name` | NVARCHAR(255) | Alert type (Deadlock, Job failure, etc.) |
| `severity` | NVARCHAR(50) | High, Medium, Low |
| `target` | NVARCHAR(255) | Monitored server/instance/database |
| `status` | NVARCHAR(50) | Raised, Ended |
| `message` | NVARCHAR(MAX) | Full description text |
| `source_ip` | NVARCHAR(50) | IP of the Redgate monitor server |
| `raw_payload` | NVARCHAR(MAX) | Exact raw JSON webhook payload |
| `sent_sms` | BIT | 1 = Dispatched to SMS |

### 2. `sms_delivery_logs` (Outgoing SMS Dispatch Store)
| Column | Type | Description |
|---|---|---|
| `id` | INT IDENTITY | Primary Key |
| `created_at` | DATETIME2 | Timestamp sent |
| `alert_name` | NVARCHAR(255) | Name of the alert |
| `target` | NVARCHAR(255) | Target server |
| `phone` | NVARCHAR(50) | Recipient mobile number |
| `message_text`| NVARCHAR(MAX) | Exact SMS text sent |
| `provider` | NVARCHAR(50) | HTTP Provider or Mock |
| `status` | NVARCHAR(50) | `sent`, `failed`, `dry_run` |
| `response_code` | NVARCHAR(50) | Gateway response code |
| `raw_response` | NVARCHAR(MAX) | Full response payload from SMS Gateway |
| `error_details` | NVARCHAR(MAX) | Error description (if failed) |

---

## ⚙️ 5. Redgate SQL Monitor Webhook Configuration

In Redgate SQL Monitor:
1. Navigate to: **Configuration** &rarr; **Notification settings** &rarr; **Webhooks**.
2. Enter the Webhook URL:
   ```
   http://<YOUR_SERVER_IP>:8000/webhook/redgate
   ```
3. Test Notification: Redgate will fire a test webhook. You will immediately see it appear in the **Live Monitor** at `http://localhost:8000`!
