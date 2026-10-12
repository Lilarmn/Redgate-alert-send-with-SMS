"""Database Manager for Redgate Alert -> SMS Relay.

Supports:
- Microsoft SQL Server (via pyodbc using ODBC Driver 17/18/SQL Server)
- Windows Authentication & SQL Server Authentication
- Automatic creation of database if it does not exist
- Automatic creation and verification of tables:
  * alerts (stores incoming webhook alerts)
  * phones (stores recipient mobile numbers)
  * sms_queue (stores outgoing SMS dispatches)
- Local SQLite database fallback
- Resilient: database timeouts or errors never block or break alert dispatching.
"""
import datetime
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("relay.db")

try:
    import pyodbc
except ImportError:
    pyodbc = None
    log.warning("pyodbc not installed. SQL Server integration will be disabled unless installed.")


def get_available_odbc_drivers() -> List[str]:
    """Return list of installed ODBC drivers."""
    if pyodbc is None:
        return []
    try:
        return pyodbc.drivers()
    except Exception:
        return []


def select_best_sql_driver(requested: Optional[str] = None) -> str:
    """Select requested driver or automatically pick the most modern installed SQL Server driver."""
    drivers = get_available_odbc_drivers()
    if requested and requested in drivers:
        return requested
    for preferred in [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "SQL Server",
    ]:
        if preferred in drivers:
            return preferred
    return requested or "SQL Server"


def _escape_odbc_val(val: Any) -> str:
    """Escape special characters in ODBC connection string parameters."""
    s = str(val if val is not None else "")
    if any(c in s for c in (";", "{", "}", " ")) or not s:
        return "{" + s.replace("}", "}}") + "}"
    return s


def build_mssql_conn_str(cfg: Dict[str, Any]) -> str:
    """Build valid ODBC connection string for SQL Server."""
    driver = select_best_sql_driver(cfg.get("driver"))
    server = cfg.get("server", ".\\SQL2025").strip()
    port = str(cfg.get("port", "")).strip()

    # Append port if specified and not standard or already in server string
    if port and port != "1433" and "," not in server and ":" not in server and "\\" not in server:
        server_str = f"{server},{port}"
    else:
        server_str = server

    database = cfg.get("database", "master").strip()
    auth_type = cfg.get("auth_type", "windows").lower()

    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server_str}",
        f"DATABASE={database}",
    ]

    # Encryption / Certificate validation for modern ODBC Drivers
    if "ODBC Driver" in driver:
        parts.append("TrustServerCertificate=yes")
        if "18" in driver:
            parts.append("Encrypt=Optional")

    if auth_type == "windows":
        parts.append("Trusted_Connection=yes")
    else:
        user = cfg.get("username", "sa")
        pwd = cfg.get("password", "")
        parts.append(f"UID={_escape_odbc_val(user)}")
        parts.append(f"PWD={_escape_odbc_val(pwd)}")

    return ";".join(parts) + ";"


class DatabaseManager:
    """Manages SQL Server and SQLite database persistence for alerts and SMS logs."""

    def __init__(self, config: Dict[str, Any], base_dir: Optional[str] = None):
        self.config = config
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.db_cfg = config.get("database", {})
        self.lock = threading.Lock()

        # Local SQLite fallback path
        storage = config.get("storage", {})
        self.sqlite_path = storage.get("database_path", "data/audit.db")
        if not os.path.isabs(self.sqlite_path):
            self.sqlite_path = os.path.join(self.base_dir, self.sqlite_path)
        os.makedirs(os.path.dirname(os.path.abspath(self.sqlite_path)), exist_ok=True)
        self._init_sqlite()

        # Circuit breaker to prevent blocking when SQL Server is offline
        self._mssql_last_failure_time: float = 0.0
        self._mssql_failure_cooldown: float = 45.0  # seconds between reconnect attempts

        # If SQL Server is enabled, auto-create DB and tables synchronously or in worker
        if self.is_mssql_enabled():
            self._try_init_mssql_database_and_tables()

    def update_config(self, config: Dict[str, Any]) -> None:
        """Update DB configuration dynamically without restarting service."""
        with self.lock:
            self.config = config
            self.db_cfg = config.get("database", {})
            self._mssql_last_failure_time = 0.0  # Reset on user config update
            if self.is_mssql_enabled():
                self._try_init_mssql_database_and_tables()

    def is_mssql_enabled(self) -> bool:
        return bool(self.db_cfg.get("enabled", False) and pyodbc is not None)

    def is_mssql_accessible(self) -> bool:
        """Return True only if MSSQL is enabled AND not in a cooldown state after failure."""
        if not self.is_mssql_enabled():
            return False
        if (time.time() - self._mssql_last_failure_time) < self._mssql_failure_cooldown:
            return False
        return True

    def mark_mssql_failure(self) -> None:
        self._mssql_last_failure_time = time.time()

    def mark_mssql_success(self) -> None:
        self._mssql_last_failure_time = 0.0

    def _init_sqlite(self) -> None:
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        received_at TEXT NOT NULL,
                        alert_id TEXT,
                        alert_name TEXT,
                        severity TEXT,
                        target TEXT,
                        status TEXT,
                        message TEXT,
                        source_ip TEXT,
                        raw_payload TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sms_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        alert_name TEXT,
                        target TEXT,
                        phone_number TEXT NOT NULL,
                        message TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        status TEXT NOT NULL,
                        response_code TEXT,
                        raw_response TEXT,
                        error_message TEXT
                    )
                """)
                conn.commit()
        except Exception as e:
            log.error("Failed to init SQLite fallback DB: %s", e)

    def _try_init_mssql_database_and_tables(self) -> None:
        try:
            res = self.ensure_mssql_database_and_tables()
            if res.get("success"):
                log.info("SQL Server database & tables verified successfully.")
            else:
                log.warning("Could not auto-verify SQL Server database/tables: %s", res.get("message"))
        except Exception as e:
            log.warning("SQL Server connection attempt at startup: %s", e)

    def test_mssql_connection(self, override_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Test SQL Server connectivity and return server version or error."""
        if pyodbc is None:
            return {"success": False, "message": "کتابخانه pyodbc نصب نیست."}

        cfg = override_cfg if override_cfg is not None else self.db_cfg
        target_db = cfg.get("database", "AlertWebhook").strip() or "AlertWebhook"
        conn_str = build_mssql_conn_str(cfg)
        timeout_sec = int(cfg.get("timeout_seconds", 5))

        try:
            conn = pyodbc.connect(conn_str, timeout=timeout_sec)
            cur = conn.cursor()
            cur.execute("SELECT @@VERSION AS ver, DB_NAME() AS current_db")
            row = cur.fetchone()
            version_str = row[0].split("\n")[0] if row else "Unknown"
            db_name = row[1] if row and len(row) > 1 else target_db
            conn.close()
            self.mark_mssql_success()
            return {
                "success": True,
                "message": f"اتصال به دیتابیس '{db_name}' با موفقیت برقرار شد.",
                "version": version_str,
                "driver": select_best_sql_driver(cfg.get("driver")),
            }
        except Exception as e:
            self.mark_mssql_failure()
            err_str = str(e)
            # If database does not exist yet on the server (Error 4060)
            if "4060" in err_str or "Cannot open database" in err_str:
                try:
                    master_cfg = dict(cfg)
                    master_cfg["database"] = "master"
                    master_conn_str = build_mssql_conn_str(master_cfg)
                    m_conn = pyodbc.connect(master_conn_str, timeout=timeout_sec)
                    m_cur = m_conn.cursor()
                    m_cur.execute("SELECT @@VERSION AS ver")
                    m_row = m_cur.fetchone()
                    version_str = m_row[0].split("\n")[0] if m_row else "Unknown"
                    m_conn.close()
                    self.mark_mssql_success()
                    return {
                        "success": True,
                        "database_exists": False,
                        "message": (
                            f"اتصال به سرویس SQL Server با موفقیت برقرار و مشخصات ورود تایید شد. "
                            f"دیتابیس '{target_db}' هنوز روی سرور ایجاد نشده است. "
                            f"روی دکمه '🛠️ بررسی و ساخت خودکار جدول‌ها' کلیک کنید تا دیتابیس و تمام جدول‌ها به طور خودکار ساخته شوند."
                        ),
                        "version": version_str,
                        "driver": select_best_sql_driver(cfg.get("driver")),
                    }
                except Exception as m_e:
                    log.warning("Connection to master failed as well: %s", m_e)
                    if "18456" in str(m_e) or "Login failed" in str(m_e):
                        return {
                            "success": False,
                            "message": (
                                f"ورود به SQL Server ناموفق بود: نام کاربری یا رمز عبور اشتباه است، یا کاربر '{cfg.get('username', 'sa')}' غیرفعال است یا احراز هویت Mixed Mode فعال نیست.\n({m_e})"
                            )
                        }

            return {"success": False, "message": err_str}

    def ensure_mssql_database_and_tables(self, override_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Automatically verify and create the database and tables in SQL Server."""
        if pyodbc is None:
            return {"success": False, "message": "pyodbc is not installed."}

        cfg = override_cfg if override_cfg is not None else self.db_cfg
        target_db = cfg.get("database", "AlertWebhook").strip() or "AlertWebhook"
        target_db_safe = target_db.replace("]", "]]")
        alerts_table = cfg.get("alerts_table", "alerts").strip() or "alerts"
        sms_table = cfg.get("sms_table", "sms_queue").strip() or "sms_queue"
        timeout_sec = int(cfg.get("timeout_seconds", 5))

        # 1. Connect to master database first to check/create the target database
        master_err = None
        try:
            master_cfg = dict(cfg)
            master_cfg["database"] = "master"
            master_conn_str = build_mssql_conn_str(master_cfg)
            conn = pyodbc.connect(master_conn_str, autocommit=True, timeout=timeout_sec)
            cur = conn.cursor()
            cur.execute(f"""
                IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = '{target_db}')
                BEGIN
                    CREATE DATABASE [{target_db_safe}]
                END
            """)
            conn.close()
            log.info("SQL Server database '%s' verified/created.", target_db)
        except Exception as e:
            master_err = str(e)
            log.warning("Notice on master DB check (proceeding to direct DB check): %s", e)

        # 2. Connect directly to target_db to check/create tables
        conn_str = build_mssql_conn_str(cfg)
        try:
            conn = pyodbc.connect(conn_str, autocommit=True, timeout=timeout_sec)
            cur = conn.cursor()

            # Table: alerts
            cur.execute(f"""
                IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = '{alerts_table}')
                BEGIN
                    CREATE TABLE dbo.[{alerts_table}] (
                        id INT IDENTITY(1,1) PRIMARY KEY,
                        source_ip NVARCHAR(50) NULL,
                        alert_id NVARCHAR(100) NULL,
                        alert_name NVARCHAR(255) NULL,
                        severity NVARCHAR(50) NULL,
                        target NVARCHAR(255) NULL,
                        status NVARCHAR(50) NULL,
                        message NVARCHAR(MAX) NULL,
                        url NVARCHAR(MAX) NULL,
                        raw_payload NVARCHAR(MAX) NULL,
                        received_at DATETIME NOT NULL DEFAULT GETDATE(),
                        processed BIT NOT NULL DEFAULT 1
                    )
                END
            """)

            # Table: phones
            cur.execute(f"""
                IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'phones')
                BEGIN
                    CREATE TABLE dbo.phones (
                        id INT IDENTITY(1,1) PRIMARY KEY,
                        name NVARCHAR(100) NULL,
                        phone_number NVARCHAR(50) NOT NULL,
                        is_active BIT NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT GETDATE(),
                        notes NVARCHAR(255) NULL
                    )
                END
            """)

            # Table: sms_queue / delivery logs
            cur.execute(f"""
                IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = '{sms_table}')
                BEGIN
                    CREATE TABLE dbo.[{sms_table}] (
                        id INT IDENTITY(1,1) PRIMARY KEY,
                        alert_id NVARCHAR(100) NULL,
                        phone_number NVARCHAR(50) NOT NULL,
                        message NVARCHAR(MAX) NOT NULL,
                        status NVARCHAR(50) NOT NULL,
                        sent_at DATETIME NULL DEFAULT GETDATE(),
                        error_message NVARCHAR(MAX) NULL,
                        created_at DATETIME NOT NULL DEFAULT GETDATE()
                    )
                END
            """)

            # Ensure alert_id and sent_at in sms_table are NULLABLE to prevent constraint 515 errors
            try:
                cur.execute(f"""
                    IF EXISTS (
                        SELECT 1 FROM sys.columns c
                        JOIN sys.tables t ON c.object_id = t.object_id
                        WHERE t.name = '{sms_table}' AND c.name = 'sent_at' AND c.is_nullable = 0
                    )
                    BEGIN
                        ALTER TABLE dbo.[{sms_table}] ALTER COLUMN sent_at DATETIME NULL;
                    END
                """)
            except Exception as alt_err:
                log.warning("Notice altering sent_at nullability in %s: %s", sms_table, alt_err)

            try:
                cur.execute(f"""
                    IF EXISTS (
                        SELECT 1 FROM sys.columns c
                        JOIN sys.tables t ON c.object_id = t.object_id
                        WHERE t.name = '{sms_table}' AND c.name = 'alert_id' AND c.is_nullable = 0
                    )
                    BEGIN
                        DECLARE @col_type NVARCHAR(50) = (
                            SELECT TYPE_NAME(system_type_id) FROM sys.columns 
                            WHERE object_id = OBJECT_ID('dbo.[{sms_table}]') AND name = 'alert_id'
                        );
                        IF @col_type = 'int'
                            ALTER TABLE dbo.[{sms_table}] ALTER COLUMN alert_id INT NULL;
                        ELSE
                            ALTER TABLE dbo.[{sms_table}] ALTER COLUMN alert_id NVARCHAR(100) NULL;
                    END
                """)
            except Exception as alt_err:
                log.warning("Notice altering alert_id nullability in %s: %s", sms_table, alt_err)

            conn.close()
            self.mark_mssql_success()
            return {
                "success": True,
                "message": f"دیتابیس '{target_db}' و جدول‌های '{alerts_table}'، 'phones' و '{sms_table}' با موفقیت بررسی و ساخته شدند.",
            }
        except Exception as e:
            err_str = str(e)
            if master_err and ("4060" in err_str or "Cannot open database" in err_str):
                return {
                    "success": False,
                    "message": (
                        f"دیتابیس '{target_db}' هنوز روی سرور وجود ندارد و تلاش برای ساخت خودکار آن در دیتابیس master با خطا مواجه شد: {master_err}. "
                        f"لطفاً بررسی کنید که کاربر '{cfg.get('username', 'sa')}' دسترسی ایجاد دیتابیس (dbcreator / sysadmin) داشته باشد، یا دیتابیس '{target_db}' را به صورت دستی در SQL Server Management Studio ایجاد فرمایید."
                    )
                }
            return {"success": False, "message": err_str}

    # Alias for compatibility
    ensure_mssql_tables = ensure_mssql_database_and_tables

    def get_active_phones_from_db(self) -> List[str]:
        """Fetch active recipient phone numbers from SQL Server 'phones' table."""
        if not self.is_mssql_accessible():
            return []
        conn_str = build_mssql_conn_str(self.db_cfg)
        try:
            conn = pyodbc.connect(conn_str, timeout=2)
            cur = conn.cursor()
            cur.execute("IF OBJECT_ID('dbo.phones', 'U') IS NOT NULL SELECT phone_number FROM dbo.phones WHERE is_active = 1")
            rows = cur.fetchall()
            conn.close()
            self.mark_mssql_success()
            return [str(r[0]).strip() for r in rows if r[0]]
        except Exception as e:
            self.mark_mssql_failure()
            log.warning("Could not read phones table from SQL Server: %s", e)
            return []

    def sync_phone_to_db(self, phone: str, name: str = "Admin") -> None:
        """Insert phone number into SQL Server phones table if not already present."""
        if not self.is_mssql_accessible():
            return
        conn_str = build_mssql_conn_str(self.db_cfg)
        try:
            conn = pyodbc.connect(conn_str, timeout=2)
            cur = conn.cursor()
            cur.execute("""
                IF OBJECT_ID('dbo.phones', 'U') IS NOT NULL
                IF NOT EXISTS (SELECT 1 FROM dbo.phones WHERE phone_number = ?)
                BEGIN
                    INSERT INTO dbo.phones (name, phone_number, is_active, created_at)
                    VALUES (?, ?, 1, GETDATE())
                END
            """, (phone, name, phone))
            conn.commit()
            conn.close()
            self.mark_mssql_success()
        except Exception as e:
            self.mark_mssql_failure()
            log.warning("Could not sync phone to SQL Server: %s", e)

    def save_alert(self, details: Dict[str, Any], raw_payload: str, source_ip: str) -> Optional[int]:
        """Save received alert into SQL Server and SQLite local database. Returns generated alert ID."""
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        generated_id = None

        # 1. Always write to SQLite local store
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO alerts (
                        received_at, alert_id, alert_name, severity, target,
                        status, message, source_ip, raw_payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now_str,
                    details.get("alert_id"),
                    details.get("alert_name"),
                    details.get("severity"),
                    details.get("target"),
                    details.get("status"),
                    details.get("message"),
                    source_ip,
                    raw_payload,
                ))
                generated_id = cur.lastrowid
                conn.commit()
        except Exception as e:
            log.error("Failed to insert alert into SQLite: %s", e)

        # 2. If SQL Server is enabled, write to SQL Server table
        if self.is_mssql_accessible():
            alerts_table = self.db_cfg.get("alerts_table", "alerts") or "alerts"
            conn_str = build_mssql_conn_str(self.db_cfg)
            try:
                conn = pyodbc.connect(conn_str, timeout=3)
                cur = conn.cursor()
                cur.execute(f"SELECT TOP 0 * FROM dbo.[{alerts_table}]")
                col_names = [c[0].lower() for c in cur.description]

                cols = ["source_ip", "alert_id", "alert_name", "severity", "target", "status", "message", "raw_payload"]
                vals = [source_ip, details.get("alert_id"), details.get("alert_name"), details.get("severity"), details.get("target"), details.get("status"), details.get("message"), raw_payload]

                if "url" in col_names:
                    cols.append("url"); vals.append(details.get("url"))
                if "processed" in col_names:
                    cols.append("processed"); vals.append(1)
                elif "sent_sms" in col_names:
                    cols.append("sent_sms"); vals.append(1)
                if "received_at" in col_names:
                    cols.append("received_at"); vals.append(datetime.datetime.now())

                cols_sql = ", ".join(f"[{c}]" for c in cols)
                placeholders = ", ".join(["?"] * len(vals))

                cur.execute(f"INSERT INTO dbo.[{alerts_table}] ({cols_sql}) OUTPUT INSERTED.id VALUES ({placeholders})", vals)
                row = cur.fetchone()
                if row and row[0]:
                    generated_id = int(row[0])
                conn.commit()
                conn.close()
                self.mark_mssql_success()
                log.info("Saved alert to SQL Server table '%s' (ID: %s)", alerts_table, generated_id)
            except Exception as e:
                self.mark_mssql_failure()
                log.error("Failed to save alert to SQL Server '%s': %s", alerts_table, e)

        return generated_id

    def save_sms_log(
        self,
        alert_name: str,
        target: str,
        phone: str,
        message_text: str,
        provider: str,
        status: str,
        response_code: Optional[str] = None,
        raw_response: Optional[str] = None,
        error_details: Optional[str] = None,
        alert_id: Optional[Any] = None,
    ) -> None:
        """Save outgoing SMS dispatch result to SQL Server and local SQLite."""
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. Always write to SQLite
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO sms_queue (
                        created_at, alert_name, target, phone_number, message,
                        provider, status, response_code, raw_response, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now_str,
                    alert_name,
                    target,
                    phone,
                    message_text,
                    provider,
                    status,
                    str(response_code or ""),
                    str(raw_response or ""),
                    str(error_details or ""),
                ))
                conn.commit()
        except Exception as e:
            log.error("Failed to insert SMS log into SQLite: %s", e)

        # 2. Write to SQL Server if enabled
        if self.is_mssql_accessible():
            sms_table = self.db_cfg.get("sms_table", "sms_queue") or "sms_queue"
            alerts_table = self.db_cfg.get("alerts_table", "alerts") or "alerts"
            conn_str = build_mssql_conn_str(self.db_cfg)
            try:
                conn = pyodbc.connect(conn_str, timeout=3)
                cur = conn.cursor()
                cur.execute(f"SELECT TOP 0 * FROM dbo.[{sms_table}]")
                col_names = [c[0].lower() for c in cur.description]

                cols = []
                vals = []

                if "alert_id" in col_names:
                    aid = None
                    if alert_id is not None:
                        try:
                            candidate_aid = int(alert_id)
                            # Check if candidate_aid exists in alerts table to prevent FK constraint failure
                            cur.execute(f"SELECT 1 FROM dbo.[{alerts_table}] WHERE id = ?", (candidate_aid,))
                            if cur.fetchone():
                                aid = candidate_aid
                        except Exception:
                            aid = None
                    cols.append("alert_id"); vals.append(aid)

                if "phone_number" in col_names:
                    cols.append("phone_number"); vals.append(phone)
                elif "phone" in col_names:
                    cols.append("phone"); vals.append(phone)

                if "message" in col_names:
                    cols.append("message"); vals.append(message_text)
                elif "message_text" in col_names:
                    cols.append("message_text"); vals.append(message_text)

                if "status" in col_names:
                    cols.append("status"); vals.append(status)

                if "alert_name" in col_names:
                    cols.append("alert_name"); vals.append(alert_name)

                if "target" in col_names:
                    cols.append("target"); vals.append(target)

                if "provider" in col_names:
                    cols.append("provider"); vals.append(provider)

                if "error_message" in col_names:
                    cols.append("error_message"); vals.append(str(error_details or ""))
                elif "error_details" in col_names:
                    cols.append("error_details"); vals.append(str(error_details or ""))

                if "raw_response" in col_names:
                    cols.append("raw_response"); vals.append(str(raw_response or ""))

                if "response_code" in col_names:
                    cols.append("response_code"); vals.append(str(response_code or ""))

                if "created_at" in col_names:
                    cols.append("created_at"); vals.append(datetime.datetime.now())

                if "sent_at" in col_names:
                    sent_col_desc = next((c for c in cur.description if c[0].lower() == "sent_at"), None)
                    is_nullable = sent_col_desc[6] if (sent_col_desc and len(sent_col_desc) > 6) else True
                    if status == "sent":
                        sent_val = datetime.datetime.now()
                    else:
                        sent_val = None if is_nullable else datetime.datetime.now()
                    cols.append("sent_at")
                    vals.append(sent_val)

                if cols:
                    cols_sql = ", ".join(f"[{c}]" for c in cols)
                    placeholders = ", ".join(["?"] * len(vals))
                    try:
                        cur.execute(f"INSERT INTO dbo.[{sms_table}] ({cols_sql}) VALUES ({placeholders})", vals)
                        conn.commit()
                    except Exception as ins_err:
                        err_str = str(ins_err)
                        handled = False
                        # If constraint 515 on sent_at, provide current datetime
                        if "515" in err_str and "sent_at" in err_str and "sent_at" in cols:
                            vals[cols.index("sent_at")] = datetime.datetime.now()
                            handled = True
                        # If FK or NULL constraint on alert_id
                        if ("547" in err_str or "FK_" in err_str or "515" in err_str) and "alert_id" in cols:
                            vals[cols.index("alert_id")] = None
                            handled = True

                        if handled:
                            try:
                                cur.execute(f"INSERT INTO dbo.[{sms_table}] ({cols_sql}) VALUES ({placeholders})", vals)
                                conn.commit()
                            except Exception:
                                # Ultimate fallback: if alert_id cannot be null, pick top 1 valid alert id
                                if "alert_id" in cols:
                                    cur.execute(f"SELECT TOP 1 id FROM dbo.[{alerts_table}] ORDER BY id DESC")
                                    existing_row = cur.fetchone()
                                    if existing_row:
                                        vals[cols.index("alert_id")] = existing_row[0]
                                if "sent_at" in cols:
                                    vals[cols.index("sent_at")] = datetime.datetime.now()
                                cur.execute(f"INSERT INTO dbo.[{sms_table}] ({cols_sql}) VALUES ({placeholders})", vals)
                                conn.commit()
                        else:
                            raise ins_err

                conn.close()
                self.mark_mssql_success()
                log.info("Saved SMS dispatch to SQL Server '%s' for %s", sms_table, phone)
            except Exception as e:
                self.mark_mssql_failure()
                log.error("Failed to save SMS log to SQL Server '%s': %s", sms_table, e)

    def get_recent_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch recent alerts for visual dashboard."""
        results = []
        if self.is_mssql_accessible():
            alerts_table = self.db_cfg.get("alerts_table", "alerts") or "alerts"
            conn_str = build_mssql_conn_str(self.db_cfg)
            try:
                conn = pyodbc.connect(conn_str, timeout=2)
                cur = conn.cursor()
                cur.execute(f"""
                    SELECT TOP (?) id,
                           alert_id, alert_name, severity, target, status, message, source_ip, raw_payload
                    FROM dbo.[{alerts_table}]
                    ORDER BY id DESC
                """, (limit,))
                cols = [c[0] for c in cur.description]
                for row in cur.fetchall():
                    results.append(dict(zip(cols, row)))
                conn.close()
                self.mark_mssql_success()
                return results
            except Exception as e:
                self.mark_mssql_failure()
                log.warning("Could not read recent alerts from SQL Server, falling back to SQLite: %s", e)

        # Fallback to SQLite
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, received_at AS created_at, alert_id, alert_name, severity, target,
                           status, message, source_ip, raw_payload
                    FROM alerts
                    ORDER BY id DESC LIMIT ?
                """, (limit,))
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            log.error("Error reading recent alerts from SQLite: %s", e)
            return []

    def get_recent_sms_logs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch recent SMS dispatches for visual dashboard."""
        results = []
        if self.is_mssql_accessible():
            sms_table = self.db_cfg.get("sms_table", "sms_queue") or "sms_queue"
            conn_str = build_mssql_conn_str(self.db_cfg)
            try:
                conn = pyodbc.connect(conn_str, timeout=2)
                cur = conn.cursor()
                cur.execute(f"SELECT TOP 0 * FROM dbo.[{sms_table}]")
                col_names = [c[0].lower() for c in cur.description]

                phone_col = "phone_number" if "phone_number" in col_names else "phone"
                msg_col = "message" if "message" in col_names else "message_text"
                err_col = "error_message" if "error_message" in col_names else ("error_details" if "error_details" in col_names else "status")
                resp_col = "response_code" if "response_code" in col_names else "status"

                cur.execute(f"""
                    SELECT TOP (?) id, {phone_col} AS phone, {msg_col} AS message_text,
                           status, {resp_col} AS response_code, {err_col} AS error_details
                    FROM dbo.[{sms_table}]
                    ORDER BY id DESC
                """, (limit,))
                cols = [c[0] for c in cur.description]
                for row in cur.fetchall():
                    results.append(dict(zip(cols, row)))
                conn.close()
                self.mark_mssql_success()
                return results
            except Exception as e:
                self.mark_mssql_failure()
                log.warning("Could not read recent SMS logs from SQL Server, falling back to SQLite: %s", e)

        # Fallback to SQLite
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, created_at, alert_name, target, phone_number AS phone, message AS message_text,
                           provider, status, response_code, raw_response, error_message AS error_details
                    FROM sms_queue
                    ORDER BY id DESC LIMIT ?
                """, (limit,))
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            log.error("Error reading recent SMS logs from SQLite: %s", e)
            return []
