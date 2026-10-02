"""
Mildew station ingest + API.

One process, two jobs:
  1. A background thread reads newline-delimited JSON from the central node's
     serial port (the central_node_LoRa firmware) and writes readings into
     SQLite. Lines that are not JSON (boot messages, diagnostics) are only
     echoed to the log, and heartbeat lines ({"gateway": ...}) update
     in-memory status only. A reading line looks like:
       {"leafNodeID":5,"readingID":70000,"realTimeLog":1790940463,
        "temperature":23.46,"humidity":100,"windSpeed":2,"leafWetness":12,
        "status":7,"rssi":-87.5,"snr":9.25}
     Its keys are the `reading` table's column names. A value the leaf node
     could not measure is left out and stored as NULL.
  2. FastAPI serves the data back to the dashboard through NGINX:
       GET /api/latest            most recent reading
       GET /api/history?hours=24  readings for the chart (max 21 days)
       GET /api/status            link health: serial state, last packet age,
                                  counts, and the last packet of each leaf node

Time handling: `realTimeLog` is when the leaf node took the reading (its RTC,
unix seconds, UTC) and is what the API sorts by. `receivedAt` is when the Pi
got it. The two differ when a leaf node could not reach the central node and
sent its queued readings later (`resentAt` is then set), so sorting by
`receivedAt` would put those readings at the wrong time. A reading whose leaf
node had no trusted clock arrives without `realTimeLog` and is placed by
`receivedAt`.
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
CREATE TABLE IF NOT EXISTS centralnode (
    centralNodeID           INTEGER PRIMARY KEY AUTOINCREMENT,
    centralNodeName         TEXT NOT NULL,
    centralNodeDescription  TEXT
);

CREATE TABLE IF NOT EXISTS leafnode (
    leafNodeID           INTEGER PRIMARY KEY AUTOINCREMENT,
    leafNodeName         TEXT NOT NULL UNIQUE,
    leafNodeLongitude    REAL,
    leafNodeLatitude     REAL,
    leafNodeStatus       BOOLEAN,
    leafNodeInstalledAt  INTEGER NOT NULL,
    leafNodeBattery      INTEGER,
    lastConnectedAt      INTEGER NOT NULL,
    blockID              INTEGER            -- REFERENCES block (blockID) once that table exists
);
CREATE INDEX IF NOT EXISTS idx_leafnode_block ON leafnode (blockID);

CREATE TABLE IF NOT EXISTS alert (
    alertID         INTEGER PRIMARY KEY AUTOINCREMENT,
    alertSeverity   INTEGER,
    alertMessage    TEXT,
    raisedAt        INTEGER NOT NULL,
    sentAt          INTEGER NOT NULL,
    isAcknowledged  BOOLEAN NOT NULL DEFAULT 0,
    resolvedAt      INTEGER,                -- NULL while the alert is open
    centralNodeID   INTEGER REFERENCES centralnode (centralNodeID)
);
CREATE INDEX IF NOT EXISTS idx_alert_raised ON alert (raisedAt);
CREATE INDEX IF NOT EXISTS idx_alert_central ON alert (centralNodeID);

CREATE TABLE IF NOT EXISTS leafnode_alert (
    bridgeID    INTEGER PRIMARY KEY AUTOINCREMENT,
    leafNodeID  INTEGER NOT NULL REFERENCES leafnode (leafNodeID),
    alertID     INTEGER NOT NULL REFERENCES alert (alertID),
    UNIQUE (leafNodeID, alertID)
);
CREATE INDEX IF NOT EXISTS idx_leafnode_alert_alert ON leafnode_alert (alertID);

CREATE TABLE IF NOT EXISTS reading (
    readingID    INTEGER PRIMARY KEY AUTOINCREMENT,
    realTimeLog  INTEGER,                   -- sender RTC epoch (as reported)
    temperature  REAL,
    humidity     REAL,
    windSpeed    REAL,
    leafWetness  REAL,
    isSent       BOOLEAN NOT NULL DEFAULT 0,
    isReceived   BOOLEAN NOT NULL DEFAULT 0,
    sentAt       INTEGER,
    receivedAt   INTEGER NOT NULL,          -- unix seconds, Pi clock (UTC)
    resentAt     INTEGER,
    leafNodeID   INTEGER NOT NULL REFERENCES leafnode (leafNodeID)
);
CREATE INDEX IF NOT EXISTS idx_reading_received ON reading (receivedAt);
"""

# ---------------------------------------------------------------------------
# in-memory link status (shared between the reader thread and the API)
status_lock = threading.Lock()
link = {
    "serial_connected": False,
    "serial_port": SERIAL_PORT,
    "last_packet_at": None,      # unix seconds
    "packets_stored": 0,
    "duplicates_dropped": 0,     # readings a leaf node sent twice (its Ack got lost)
    "nodes": {},                 # leafNodeID -> its last packet: readingID, rssi, snr, status
    "gateway_heartbeat": None,   # last {"gateway": ...} line, verbatim
    "started_at": int(time.time()),
}


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


READING_COLUMNS = ("realTimeLog", "temperature", "humidity", "windSpeed", "leafWetness",
                   "isSent", "isReceived", "sentAt", "receivedAt", "resentAt", "leafNodeID")

# What the API sorts and filters by: when the reading was taken.
READING_TIME = "COALESCE(realTimeLog, receivedAt)"


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db_connect() as conn:
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS leaves an older table as it is. Say so
        # here rather than failing on every reading later.
        have = {row["name"] for row in conn.execute("PRAGMA table_info(reading)")}
        missing = [c for c in READING_COLUMNS if c not in have]
        if missing:
            print(f"ingest: DATABASE IS FROM AN OLDER VERSION - table 'reading' lacks {missing}. "
                  "Readings cannot be stored until it is recreated: "
                  "docker compose down -v && docker compose up -d", flush=True)


def store_reading(conn: sqlite3.Connection, d: dict) -> bool:
    """Store one reading line from the central node. False = already stored."""
    now = int(time.time())
    node_id = d["leafNodeID"]
    real_time = d.get("realTimeLog")

    # reading.leafNodeID is required, so a node's first packet registers it,
    # under the ID the node itself transmits.
    conn.execute(
        """INSERT OR IGNORE INTO leafnode
           (leafNodeID, leafNodeName, leafNodeStatus, leafNodeInstalledAt, lastConnectedAt)
           VALUES (?,?,1,?,?)""",
        (node_id, f"leaf-{node_id}", now, now),
    )
    conn.execute(
        "UPDATE leafnode SET leafNodeStatus = 1, lastConnectedAt = ? WHERE leafNodeID = ?",
        (now, node_id),
    )

    # A leaf node repeats a reading when it misses the Ack, and the central
    # node only remembers the last few it passed on. A node takes one reading
    # per time slot, so the same node and time is the same reading.
    duplicate = real_time is not None and conn.execute(
        "SELECT 1 FROM reading WHERE leafNodeID = ? AND realTimeLog = ?",
        (node_id, real_time),
    ).fetchone() is not None

    if not duplicate:
        conn.execute(
            """INSERT INTO reading
               (realTimeLog, temperature, humidity, windSpeed, leafWetness,
                isSent, isReceived, sentAt, receivedAt, resentAt, leafNodeID)
               VALUES (?,?,?,?,?,1,1,?,?,?,?)""",
            (
                real_time,
                d.get("temperature"),   # absent keys stay NULL: the leaf node
                d.get("humidity"),      # leaves out values it could not
                d.get("windSpeed"),     # measure, and the chart renders NULL
                d.get("leafWetness"),   # as a line gap
                real_time,              # first sent as soon as it was taken
                now,
                d.get("resentAt"),
                node_id,
            ),
        )
    conn.commit()

    with status_lock:
        link["last_packet_at"] = now
        link["duplicates_dropped" if duplicate else "packets_stored"] += 1
        link["nodes"][node_id] = {
            "last_packet_at": now,
            "readingID": d.get("readingID"),   # the node's own count
            "status": d.get("status"),         # bits: 1 sensor ok, 2 clock ok, 4 SD ok, 8 resend
            "rssi": d.get("rssi"),
            "snr": d.get("snr"),
        }
    return not duplicate


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
                    if not isinstance(d, dict):
                        continue
                    if "gateway" in d:
                        with status_lock:
                            link["gateway_heartbeat"] = d
                        continue
                    if isinstance(d.get("leafNodeID"), int):
                        # A reading that cannot be stored must not end this
                        # thread: nothing would be stored again until a restart.
                        try:
                            stored = store_reading(conn, d)
                        except sqlite3.Error as exc:
                            conn.rollback()
                            print(f"ingest: DATABASE ERROR, reading lost ({exc}): {line[:200]}", flush=True)
                            continue
                        print(f"ingest: {'stored' if stored else 'duplicate of'} "
                              f"node={d['leafNodeID']} reading={d.get('readingID')} "
                              f"t={d.get('temperature')} rh={d.get('humidity')} "
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


@app.get("/api/latest")
def latest() -> dict:
    with db_connect() as conn:
        row = conn.execute(
            f"SELECT * FROM reading ORDER BY {READING_TIME} DESC, readingID DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No readings yet")
    return dict(row)


@app.get("/api/history")
def history(hours: float = Query(24, gt=0, le=504)) -> dict:
    since = int(time.time() - hours * 3600)
    with db_connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM reading WHERE {READING_TIME} >= ? "
            f"ORDER BY {READING_TIME} ASC, readingID ASC",
            (since,),
        ).fetchall()
    return {"hours": hours, "count": len(rows),
            "reading": [dict(r) for r in rows]}


@app.get("/api/status")
def status() -> dict:
    with status_lock:
        snap = dict(link)
        snap["nodes"] = {node: dict(last) for node, last in link["nodes"].items()}
    now = int(time.time())
    snap["server_time"] = now
    snap["last_packet_age_s"] = (
        None if snap["last_packet_at"] is None else now - snap["last_packet_at"]
    )
    return snap
