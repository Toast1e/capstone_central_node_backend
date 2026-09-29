"""
Mildew station ingest + API.

One process, two jobs:
  1. A background thread reads newline-delimited JSON from the LoRa gateway's
     serial port and writes readings into SQLite. Non-JSON lines (boot
     messages, "GW:" diagnostics) are silently skipped, and heartbeat lines
     ({"gateway": ...}) update in-memory status only.
  2. FastAPI serves the data back to the dashboard through NGINX:
       GET /api/latest            most recent reading
       GET /api/history?hours=24  readings for the chart (max 21 days)
       GET /api/status            link health: serial state, last packet age,
                                  counts, estimated missed packets (seq gaps)

Time handling: `received_at` is the Pi's clock (unix seconds, UTC) and is what
the dashboard charts. The sender's own `ts` (its RTC, set to local time at
flash) is stored untouched for cross-checking against the SD card CSVs.
"""

import json
import os
import sqlite3
import threading
import time

import serial  # pyserial
from fastapi import FastAPI, HTTPException, Query

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/lora")
BAUD = int(os.environ.get("BAUD", "115200"))
DB_PATH = os.environ.get("DB_PATH", "/data/weather.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  INTEGER NOT NULL,          -- unix seconds, Pi clock (UTC)
    node         TEXT,
    seq          INTEGER,
    ts           INTEGER,                   -- sender RTC epoch (as reported)
    temp_c       REAL,
    humidity_pct REAL,
    dewpoint_c   REAL,
    battery_v    REAL,
    battery_pct  REAL,
    sd           INTEGER,
    rssi         REAL,
    snr          REAL
);
CREATE INDEX IF NOT EXISTS idx_readings_received ON readings (received_at);
"""

# ---------------------------------------------------------------------------
# in-memory link status (shared between the reader thread and the API)
status_lock = threading.Lock()
link = {
    "serial_connected": False,
    "serial_port": SERIAL_PORT,
    "last_packet_at": None,      # unix seconds
    "last_seq": None,
    "packets_stored": 0,
    "seq_gaps_missed": 0,        # readings the sender counted but we never got
    "gateway_heartbeat": None,   # last {"gateway": ...} line, verbatim
    "started_at": int(time.time()),
}


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db_connect() as conn:
        conn.executescript(SCHEMA)


def store_reading(conn: sqlite3.Connection, d: dict) -> None:
    now = int(time.time())
    conn.execute(
        """INSERT INTO readings
           (received_at, node, seq, ts, temp_c, humidity_pct, dewpoint_c,
            battery_v, battery_pct, sd, rssi, snr)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            now,
            d.get("node"),
            d.get("seq"),
            d.get("ts"),
            d.get("temp_c"),        # absent keys stay NULL — the sender omits
            d.get("humidity_pct"),  # failed sensor values on purpose, and the
            d.get("dewpoint_c"),    # chart renders NULL as a line gap
            d.get("battery_v"),
            d.get("battery_pct"),
            None if d.get("sd") is None else int(bool(d.get("sd"))),
            d.get("rssi"),
            d.get("snr"),
        ),
    )
    conn.commit()

    with status_lock:
        link["last_packet_at"] = now
        link["packets_stored"] += 1
        seq = d.get("seq")
        prev = link["last_seq"]
        if isinstance(seq, int):
            if isinstance(prev, int) and seq > prev + 1:
                link["seq_gaps_missed"] += seq - prev - 1
            link["last_seq"] = seq


def serial_reader() -> None:
    """Forever: open the port, consume lines, survive unplugs."""
    conn = db_connect()
    while True:
        try:
            with serial.Serial(SERIAL_PORT, BAUD, timeout=5) as port:
                with status_lock:
                    link["serial_connected"] = True
                print(f"ingest: listening on {SERIAL_PORT} @ {BAUD}", flush=True)
                while True:
                    raw = port.readline()
                    if not raw:
                        continue  # timeout tick — nothing sent in 5 s, normal
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("{"):
                        if line:
                            print(f"ingest: [gw] {line}", flush=True)
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        print(f"ingest: bad JSON dropped: {line[:120]}", flush=True)
                        continue
                    if "gateway" in d:
                        with status_lock:
                            link["gateway_heartbeat"] = d
                        continue
                    if "node" in d:
                        store_reading(conn, d)
                        print(f"ingest: stored seq={d.get('seq')} "
                              f"t={d.get('temp_c')} rh={d.get('humidity_pct')} "
                              f"rssi={d.get('rssi')}", flush=True)
        except (serial.SerialException, OSError) as exc:
            with status_lock:
                link["serial_connected"] = False
            print(f"ingest: serial error ({exc}) — retrying in 5 s", flush=True)
            time.sleep(5)


# ---------------------------------------------------------------------------
app = FastAPI(title="Mildew station API")


@app.on_event("startup")
def startup() -> None:
    init_db()
    threading.Thread(target=serial_reader, daemon=True).start()


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("sd") is not None:
        d["sd"] = bool(d["sd"])
    return d


@app.get("/api/latest")
def latest() -> dict:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM readings ORDER BY received_at DESC, id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No readings yet")
    return row_to_dict(row)


@app.get("/api/history")
def history(hours: float = Query(24, gt=0, le=504)) -> dict:
    since = int(time.time() - hours * 3600)
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM readings WHERE received_at >= ? "
            "ORDER BY received_at ASC",
            (since,),
        ).fetchall()
    return {"hours": hours, "count": len(rows),
            "readings": [row_to_dict(r) for r in rows]}


@app.get("/api/status")
def status() -> dict:
    with status_lock:
        snap = dict(link)
    now = int(time.time())
    snap["server_time"] = now
    snap["last_packet_age_s"] = (
        None if snap["last_packet_at"] is None else now - snap["last_packet_at"]
    )
    return snap
