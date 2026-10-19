import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH") or os.path.join(BASE_DIR, "data", "property.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    whatsapp TEXT,
    unit_id INTEGER REFERENCES units(id),
    lease_start TEXT,
    lease_end TEXT,
    rent_amount REAL NOT NULL DEFAULT 0,
    rent_due_day INTEGER NOT NULL DEFAULT 1,
    deposit REAL DEFAULT 0,
    send_email INTEGER NOT NULL DEFAULT 1,
    send_whatsapp INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    -- Rent is only tracked from the month the tenant was added (no false "late" alerts for older months).
    track_from TEXT NOT NULL DEFAULT (strftime('%Y-%m', 'now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    amount REAL NOT NULL,
    method TEXT NOT NULL,
    reference TEXT,
    paid_on TEXT NOT NULL,
    period TEXT NOT NULL,
    receipt_no TEXT UNIQUE,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS service_items (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER REFERENCES units(id),
    name TEXT NOT NULL,
    interval_months INTEGER NOT NULL DEFAULT 6,
    last_serviced TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS service_log (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES service_items(id) ON DELETE CASCADE,
    serviced_on TEXT NOT NULL,
    cost REAL,
    notes TEXT
);

-- Remembers which alerts were already sent so nobody gets the same message twice.
CREATE TABLE IF NOT EXISTS sent_alerts (
    key TEXT PRIMARY KEY,
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    conn.close()
