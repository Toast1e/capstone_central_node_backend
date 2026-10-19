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

import csv
import io
import json
import os
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from datetime import date, datetime, time as dtime, timedelta, timezone

import serial  # pyserial
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/lora")
BAUD = int(os.environ.get("BAUD", "115200"))
# Set SERIAL_ENABLED=0 to run the API without the radio (local testing, no /dev/lora).
SERIAL_ENABLED = os.environ.get("SERIAL_ENABLED", "1") != "0"
DB_PATH = os.environ.get("DB_PATH", "/data/weather.db")

# South Africa has no daylight saving, so a fixed offset is enough (and the
# container needs no tzdata). Used for export day boundaries and CSV timestamps.
LOCAL_TZ = timezone(timedelta(hours=int(os.environ.get("TZ_OFFSET_HOURS", "2"))))
# Nodes page: no packet for this long = NO SIGNAL. Keep it above your longest
# reading interval (the Profile page offers up to 60 min).
OFFLINE_AFTER_S = int(os.environ.get("OFFLINE_AFTER_S", str(150 * 60)))
# Nodes page: last RSSI below this (dBm) = WEAK SIGNAL.
WEAK_RSSI = float(os.environ.get("WEAK_RSSI", "-110"))

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
    blockID              INTEGER,           -- REFERENCES block (blockID) once that table exists
    leafNodeRow          INTEGER,           -- row within the block (Nodes pop-up)
    lastRssi             REAL,              -- last packet's signal strength in dBm; kept in the DB so it survives restarts
    lastSnr              REAL
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
    centralNodeID   INTEGER REFERENCES centralnode (centralNodeID),
    readingID       INTEGER REFERENCES reading (readingID)   -- the reading that triggered the alert
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
    battery      INTEGER,               -- leaf node battery %, as reported with the reading
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


READING_COLUMNS = ("realTimeLog", "temperature", "humidity", "windSpeed", "leafWetness", "battery",
                   "isSent", "isReceived", "sentAt", "receivedAt", "resentAt", "leafNodeID")

# What the API sorts and filters by: when the reading was taken.
READING_TIME = "COALESCE(realTimeLog, receivedAt)"


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db_connect() as conn:
        conn.executescript(SCHEMA)
        # Columns added after the first release are added in place, so an
        # existing database keeps its readings.
        for table, column, ddl in (
            ("reading", "battery", "INTEGER"),
            ("leafnode", "leafNodeRow", "INTEGER"),
            ("leafnode", "lastRssi", "REAL"),
            ("leafnode", "lastSnr", "REAL"),
            ("alert", "readingID", "INTEGER REFERENCES reading (readingID)"),
        ):
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        # The Nodes page and Profile rename need a central node row to read/edit.
        if conn.execute("SELECT 1 FROM centralnode LIMIT 1").fetchone() is None:
            conn.execute(
                "INSERT INTO centralnode (centralNodeName, centralNodeDescription) VALUES (?, ?)",
                ("Central Node 1", "Vineyard gateway"),
            )
        conn.commit()
        # CREATE TABLE IF NOT EXISTS leaves an older table as it is. Say so
        # here rather than failing on every reading later.
        have = {row["name"] for row in conn.execute("PRAGMA table_info(reading)")}
        missing = [c for c in READING_COLUMNS if c not in have]
        if missing:
            print(f"ingest: DATABASE IS FROM AN OLDER VERSION - table 'reading' lacks {missing}. "
                  "Readings cannot be stored until it is recreated: "
                  "docker compose down -v && docker compose up -d", flush=True)


# Function for calculating risk severity.
def calculate_risk(temperature, humidity, leaf_wetness):
    """
    Calculate mildew risk based on the environmental reading.

    Returns:
        2 = High risk
        1 = Moderate risk
        0 = Low risk
        None = Conditions do not qualify as an alert
    """

    # We cannot calculate a meaningful risk if the important
    # environmental readings are missing.
    if temperature is None or humidity is None or leaf_wetness is None:
        return None

    # HIGH RISK
    # Warm temperatures combined with very high humidity
    # and significant leaf wetness.
    if (
        15 <= temperature <= 25
        and humidity >= 90
        and leaf_wetness >= 60
    ):
        return 2

    # MODERATE RISK
    # Conditions are becoming favourable for mildew,
    # but are not severe enough for high risk.
    elif (
        15 <= temperature <= 28
        and humidity >= 80
        and leaf_wetness >= 40
    ):
        return 1

    # LOW RISK
    # There are some favourable conditions, but the
    # environment is not sufficiently risky for moderate/high risk.
    elif (
        humidity >= 70
        and leaf_wetness >= 20
    ):
        return 0

    # No meaningful mildew risk.
    # Therefore, nothing will be inserted into alert.
    else:
        return None


# After an alert is acknowledged, the same conditions will not raise another
# one for this long. Without a cooldown, every reading while the conditions
# last would create a new alert (hundreds per night per node).
ALERT_COOLDOWN_S = int(os.environ.get("ALERT_COOLDOWN_S", str(6 * 3600)))


def alert_already_covered(conn: sqlite3.Connection, node_id: int, severity: int, now: int) -> bool:
    """
    True if this leaf node already has an alert at this severity or higher
    that is still open (not acknowledged), or that was acknowledged within
    the cooldown. A HIGHER severity than any existing alert is not covered,
    so low -> moderate -> high each raise one alert.
    """
    return conn.execute(
        "SELECT 1 FROM alert a JOIN leafnode_alert la ON la.alertID = a.alertID "
        "WHERE la.leafNodeID = ? AND a.alertSeverity >= ? "
        "AND (a.isAcknowledged = 0 OR a.resolvedAt >= ?) LIMIT 1",
        (node_id, severity, now - ALERT_COOLDOWN_S),
    ).fetchone() is not None


# Store the readings in sqlite
def store_reading(conn: sqlite3.Connection, d: dict) -> bool:
    """Store one reading and create an alert when the risk threshold is met."""

    now = int(time.time())
    node_id = d["leafNodeID"]
    real_time = d.get("realTimeLog")

    # reading.leafNodeID is required, so a node's first packet registers it.
    conn.execute(
        """INSERT OR IGNORE INTO leafnode
           (leafNodeID, leafNodeName, leafNodeStatus, leafNodeInstalledAt, lastConnectedAt)
           VALUES (?,?,1,?,?)""",
        (node_id, f"leaf-{node_id}", now, now),
    )

    # Update the node's latest connection information.
    # Optional values only overwrite the existing value when
    # the packet actually contains them.
    conn.execute(
        "UPDATE leafnode SET leafNodeStatus = 1, lastConnectedAt = ?, "
        "leafNodeBattery = COALESCE(?, leafNodeBattery), "
        "lastRssi = COALESCE(?, lastRssi), lastSnr = COALESCE(?, lastSnr) "
        "WHERE leafNodeID = ?",
        (
            now,
            d.get("battery"),
            d.get("rssi"),
            d.get("snr"),
            node_id,
        ),
    )

    # Check whether this reading has already been stored.
    # This prevents a retransmitted packet from creating
    # another reading AND another alert.
    duplicate = real_time is not None and conn.execute(
        "SELECT 1 FROM reading WHERE leafNodeID = ? AND realTimeLog = ?",
        (node_id, real_time),
    ).fetchone() is not None

    if not duplicate:

        # ---------------------------------------------------------
        # STORE THE SENSOR READING
        # ---------------------------------------------------------

        cursor = conn.execute(
            """INSERT INTO reading
               (realTimeLog, temperature, humidity, windSpeed, leafWetness, battery,
                isSent, isReceived, sentAt, receivedAt, resentAt, leafNodeID)
               VALUES (?,?,?,?,?,?,1,1,?,?,?,?)""",
            (
                real_time,
                d.get("temperature"),
                d.get("humidity"),
                d.get("windSpeed"),
                d.get("leafWetness"),
                d.get("battery"),
                real_time,
                now,
                d.get("resentAt"),
                node_id,
            ),
        )

        reading_id = cursor.lastrowid

        # ---------------------------------------------------------
        # CALCULATE MILDEW RISK
        # ---------------------------------------------------------

        severity = calculate_risk(
            temperature=d.get("temperature"),
            humidity=d.get("humidity"),
            leaf_wetness=d.get("leafWetness"),
        )

        # Only create an alert if the reading qualifies as
        # low, moderate, or high risk, AND this node does not already
        # have an open (or recently acknowledged) alert at that level.
        #
        # None means the conditions do not qualify as an alert.
        if severity is not None and not alert_already_covered(conn, node_id, severity, now):

            # Create a human-readable message based on severity.
            if severity == 2:
                message = "High mildew risk detected."
            elif severity == 1:
                message = "Moderate mildew risk detected."
            else:
                message = "Low mildew risk detected."

            # -----------------------------------------------------
            # CREATE ALERT
            # -----------------------------------------------------

            alert_cursor = conn.execute(
                """INSERT INTO alert
                   (alertSeverity, alertMessage, raisedAt, sentAt,
                    isAcknowledged, centralNodeID, readingID)
                   VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    severity,
                    message,
                    real_time if real_time is not None else now,
                    now,
                    1,
                    reading_id,
                ),
            )

            # SQLite gives us the ID of the newly-created alert.
            alert_id = alert_cursor.lastrowid

            # -----------------------------------------------------
            # LINK ALERT TO THE LEAF NODE
            # -----------------------------------------------------

            conn.execute(
                """INSERT INTO leafnode_alert
                   (leafNodeID, alertID)
                   VALUES (?, ?)""",
                (node_id, alert_id),
            )

    # Commit the reading AND any alert/relationship created above
    # as one database transaction.
    conn.commit()

    # Update in-memory connection status.
    with status_lock:
        link["last_packet_at"] = now
        link["duplicates_dropped" if duplicate else "packets_stored"] += 1

        link["nodes"][node_id] = {
            "last_packet_at": now,
            "readingID": d.get("readingID"),
            "status": d.get("status"),
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


# for local testing
from fastapi.middleware.cors import CORSMiddleware


#Only added for testing locally
# Remove once Pi is being used
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://192.168.1.60:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # lets the Export page read the file name
)


# ---------- Home Page ----------

@app.get("/api/leafnodes")
def get_leaf_nodes(ids: str | None = None) -> dict:
    '''
    Return leaf nodes by ID or retrieve all if no ID passed
        GET /api/leafnodes            -> all leaf nodes, 50 latest readings each
        GET /api/leafnodes?ids=1,2,3  -> just those nodes, 50 latest readings each
    '''
    READINGS_PER_NODE = 50
    # check for if a leafNodeID/s is passed
    node_ids = [i.strip() for i in ids.split(",") if i.strip()] if ids else None

    # if leafNodeID/s passed, retrieve node/s by their ID, otherwise return all
    with db_connect() as conn:
        if node_ids:
            placeholders = ", ".join("?" for _ in node_ids)  # string of "?" for each nodeID
            nodes = conn.execute(
                f"SELECT * FROM leafnode WHERE leafNodeID IN ({placeholders})",
                node_ids,
            ).fetchall()
        else:
            nodes = conn.execute("SELECT * FROM leafnode").fetchall()

        result = []
        for n in nodes:
            row = dict(n)
            readings = conn.execute(
                "SELECT * FROM reading WHERE leafNodeID = ? "
                "ORDER BY receivedAt DESC, readingID DESC LIMIT ?",
                (row["leafNodeID"], READINGS_PER_NODE),
            ).fetchall()
            row["readingCount"] = len(readings)
            row["readings"] = [dict(r) for r in readings]
            result.append(row)

    if node_ids and not result:
        raise HTTPException(status_code=404, detail="No matching leaf nodes found")

    return {"count": len(result), "leafNodes": result}


# ---------- Alerts Page ----------

def fetch_alerts(conn: sqlite3.Connection, acknowledged: bool) -> list[dict]:
    """
    Alerts in the shape the Alerts page uses: the alert's own columns, the
    sensor `readings` that triggered it (all null for alerts raised before
    readingID was stored), and the `leafNodes` it belongs to.
    """
    order = "a.resolvedAt DESC" if acknowledged else "a.raisedAt DESC"
    rows = conn.execute(
        f"""SELECT a.*, r.temperature, r.humidity, r.leafWetness, r.windSpeed
            FROM alert a
            LEFT JOIN reading r ON r.readingID = a.readingID
            WHERE a.isAcknowledged = ?
            ORDER BY {order}""",
        (1 if acknowledged else 0,),
    ).fetchall()

    result = []
    for row in rows:
        alert = dict(row)
        alert["isAcknowledged"] = bool(alert["isAcknowledged"])
        alert["readings"] = {
            "temperature": alert.pop("temperature"),
            "humidity": alert.pop("humidity"),
            "leafWetness": alert.pop("leafWetness"),
            "windSpeed": alert.pop("windSpeed"),
        }
        nodes = conn.execute(
            """SELECT ln.leafNodeID, ln.leafNodeName
               FROM leafnode_alert la
               JOIN leafnode ln ON ln.leafNodeID = la.leafNodeID
               WHERE la.alertID = ?""",
            (alert["alertID"],),
        ).fetchall()
        alert["leafNodes"] = [dict(n) for n in nodes]
        result.append(alert)
    return result


@app.get("/api/alerts")
def get_alerts() -> dict:
    """
    Return all alerts that have not yet been acknowledged.

    GET /api/alerts
    """
    with db_connect() as conn:
        result = fetch_alerts(conn, acknowledged=False)
    return {"count": len(result), "alerts": result}


@app.patch("/api/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: int) -> dict:
    """
    Mark an alert as acknowledged, and stamp resolvedAt at the same time.
    """
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT alertID FROM alert WHERE alertID = ?", (alert_id,)
        ).fetchone()
        if existing is None:
            raise HTTPException(404, "Alert not found")

        now = int(time.time())
        conn.execute(
            "UPDATE alert SET isAcknowledged = 1, resolvedAt = ? WHERE alertID = ?",
            (now, alert_id),
        )
        conn.commit()

        # return updated value if needed to use
        updated = conn.execute(
            "SELECT * FROM alert WHERE alertID = ?", (alert_id,)
        ).fetchone()

    row = dict(updated)
    row["isAcknowledged"] = bool(row["isAcknowledged"])
    return row


@app.get("/api/alerts/acknowledged")
def get_acknowledged_alerts() -> dict:
    """
    Return all alerts that have been acknowledged.

    GET /api/alerts/acknowledged
    """
    with db_connect() as conn:
        result = fetch_alerts(conn, acknowledged=True)
    return {"count": len(result), "alerts": result}


# ---------- Nodes Page ----------

def connection_quality(last_connected_at, rssi, now: int) -> str:
    """GOOD / LOW / NONE, the three states the Nodes page draws."""
    if last_connected_at is None or now - last_connected_at > OFFLINE_AFTER_S:
        return "NONE"
    if rssi is not None and rssi < WEAK_RSSI:
        return "LOW"
    return "GOOD"


@app.get("/api/leafnodes/status")
def get_leaf_node_status() -> dict:
    """
    Every leaf node's current status, no reading history. The cards on the
    Nodes page.

    `connection` is worked out here, not stored: NONE when nothing has been
    heard for OFFLINE_AFTER_S, LOW when the last RSSI is below WEAK_RSSI,
    otherwise GOOD. `rssi` / `snr` are the last packet's values (stored, so
    they survive a restart; null until the node's first packet carries them).

    GET /api/leafnodes/status
    """
    now = int(time.time())
    with db_connect() as conn:
        nodes = conn.execute(
            "SELECT leafNodeID, leafNodeName, leafNodeStatus, leafNodeBattery, "
            "lastConnectedAt, leafNodeInstalledAt, blockID, leafNodeRow, "
            "lastRssi AS rssi, lastSnr AS snr "
            "FROM leafnode ORDER BY leafNodeID ASC"
        ).fetchall()

    result = []
    for n in nodes:
        row = dict(n)
        row["connection"] = connection_quality(row["lastConnectedAt"], row["rssi"], now)
        result.append(row)
    return {"count": len(result), "serverTime": now, "leafNodes": result}


@app.get("/api/leafnodes/{leaf_node_id}/details")
def get_leaf_node_details(leaf_node_id: int) -> dict:
    """
    One leaf node's pop-up on the Nodes page: its newest reading, signal,
    when it last reported, and where it is.

    `signal` is null when the node has no signal (the pop-up then shows
    "None"). `location` is null until a block has been set for the node.
    Times are unix seconds; the frontend formats them.

    GET /api/leafnodes/5/details
    """
    now = int(time.time())
    with db_connect() as conn:
        node = conn.execute(
            "SELECT leafNodeID, leafNodeName, leafNodeBattery, lastConnectedAt, "
            "blockID, leafNodeRow, leafNodeLatitude, leafNodeLongitude, "
            "lastRssi AS rssi, lastSnr AS snr "
            "FROM leafnode WHERE leafNodeID = ?",
            (leaf_node_id,),
        ).fetchone()
        if node is None:
            raise HTTPException(404, "Leaf node not found")
        reading = conn.execute(
            f"SELECT temperature, humidity, leafWetness, windSpeed, "
            f"{READING_TIME} AS readingTime FROM reading WHERE leafNodeID = ? "
            f"ORDER BY {READING_TIME} DESC, readingID DESC LIMIT 1",
            (leaf_node_id,),
        ).fetchone()

    connection = connection_quality(node["lastConnectedAt"], node["rssi"], now)
    reading = dict(reading) if reading else {}
    has_location = node["blockID"] is not None

    return {
        "leafNodeID": node["leafNodeID"],
        "leafNodeName": node["leafNodeName"],
        "battery": node["leafNodeBattery"],
        "connection": connection,
        "signal": None if connection == "NONE" else node["rssi"],
        "snr": None if connection == "NONE" else node["snr"],
        "lastReadingAt": reading.get("readingTime"),
        "readings": {
            "temperature": reading.get("temperature"),
            "humidity": reading.get("humidity"),
            "leafWetness": reading.get("leafWetness"),
            "windSpeed": reading.get("windSpeed"),
        },
        "location": {
            "block": node["blockID"],
            "row": node["leafNodeRow"],
            "latitude": node["leafNodeLatitude"],
            "longitude": node["leafNodeLongitude"],
        } if has_location else None,
    }


class LocationUpdate(BaseModel):
    blockID: int | None = None
    row: int | None = None
    latitude: float | None = None
    longitude: float | None = None


# client field -> leafnode column
LOCATION_COLUMNS = {
    "blockID": "blockID",
    "row": "leafNodeRow",
    "latitude": "leafNodeLatitude",
    "longitude": "leafNodeLongitude",
}


@app.patch("/api/leafnodes/{leaf_node_id}/location")
def set_leaf_node_location(leaf_node_id: int, body: LocationUpdate) -> dict:
    """
    Set where a leaf node is. Only the fields sent are changed; send null to
    clear one.

    PATCH /api/leafnodes/5/location   {"blockID": 2, "row": 7}
    """
    sent = body.model_dump(exclude_unset=True) if hasattr(body, "model_dump") \
        else body.dict(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to update")

    assignments = ", ".join(f"{LOCATION_COLUMNS[k]} = ?" for k in sent)
    with db_connect() as conn:
        cur = conn.execute(
            f"UPDATE leafnode SET {assignments} WHERE leafNodeID = ?",
            [*sent.values(), leaf_node_id],
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Leaf node not found")
    return {"leafNodeID": leaf_node_id, **sent}


@app.get("/api/centralnode")
def get_central_node() -> dict:
    """
    The central node card on the Nodes page. hotspot and phonesConnected are
    null: the API runs in a container and cannot see the Pi's hotspot.

    GET /api/centralnode
    """
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM centralnode ORDER BY centralNodeID ASC LIMIT 1"
        ).fetchone()
        conn.execute("SELECT 1").fetchone()  # database answers

    usage = shutil.disk_usage(os.path.dirname(DB_PATH))
    try:
        with open("/proc/uptime") as f:  # host uptime, also inside a container
            uptime_s = int(float(f.read().split()[0]))
    except OSError:
        uptime_s = None
    with status_lock:
        serial_ok = link["serial_connected"]

    return {
        "centralNodeID": row["centralNodeID"],
        "name": row["centralNodeName"],
        "description": row["centralNodeDescription"],
    }


class CentralNodeRename(BaseModel):
    name: str = Field(max_length=50)


@app.patch("/api/centralnode")
def rename_central_node(body: CentralNodeRename) -> dict:
    """
    Rename the central node. The name shows on the Home, Alerts, Nodes and
    Login pages, which all read it from GET /api/centralnode.

    PATCH /api/centralnode   {"name": "North Vineyard"}
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Name can't be empty")
    with db_connect() as conn:
        row = conn.execute(
            "SELECT centralNodeID FROM centralnode ORDER BY centralNodeID ASC LIMIT 1"
        ).fetchone()
        if row is None:
            raise HTTPException(404, "No central node found")
        conn.execute(
            "UPDATE centralnode SET centralNodeName = ? WHERE centralNodeID = ?",
            (name, row["centralNodeID"]),
        )
        conn.commit()
    return {"centralNodeID": row["centralNodeID"], "name": name}


# ---------- Export Page ----------

# Client-facing field name -> actual reading column (a whitelist: only these
# names ever reach the SQL).
ALLOWED_READING_FIELDS = {
    "temperature": "temperature",
    "humidity": "humidity",
    "windSpeed": "windSpeed",
    "leafWetness": "leafWetness",
    "battery": "battery",
}

# Always included, regardless of what the client asked for, so every
# exported row can still be identified, placed in time, and traced to a node.
BASE_READING_FIELDS = ["readingID", "leafNodeID", "realTimeLog", "receivedAt"]


def local_time_text(ts: int | None) -> str | None:
    """Unix seconds -> 'YYYY-MM-DD HH:MM:SS' in local time, readable in Excel."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


@app.get("/api/export/readings")
def export_readings(
    ids: str | None = None,
    fields: str | None = None,
    # the two date parameters are required, no default
    date_from: date = Query(..., alias="from"),
    date_to: date = Query(..., alias="to"),
    file_format: str = Query("json", alias="format"),
):
    """
    Return readings for one or more leaf nodes, with a chosen set of
    fields, filtered to a date range (local days, inclusive). Backs the
    Export page. Always sent as a download.

    GET /api/export/readings?ids=1,3&fields=temperature,humidity&from=2026-01-01&to=2026-01-24&format=csv
    GET /api/export/readings?fields=leafWetness,windSpeed&from=2026-01-01&to=2026-01-24
        (no "ids" -> all leaf nodes, matches the "All Leaf Nodes" UI option)
        (no "fields" -> every field; format defaults to json)
    """
    # --- validate ---
    if date_from > date_to:
        raise HTTPException(400, "'from' date must be before or equal to 'to' date")
    if file_format not in ("csv", "json"):
        raise HTTPException(400, "'format' must be 'csv' or 'json'")

    if fields is None:
        requested = list(ALLOWED_READING_FIELDS.keys())
    else:
        requested = [f.strip() for f in fields.split(",") if f.strip()]
        if not requested:
            raise HTTPException(400, "No fields selected")
        invalid = set(requested) - ALLOWED_READING_FIELDS.keys()
        if invalid:
            raise HTTPException(400, f"Unknown fields: {sorted(invalid)}")

    node_ids = None
    if ids:
        try:
            node_ids = [int(i) for i in ids.split(",") if i.strip()]
        except ValueError:
            raise HTTPException(400, "'ids' must be comma-separated integers")

    # --- local days -> Unix seconds ---
    start = int(datetime.combine(date_from, dtime.min, tzinfo=LOCAL_TZ).timestamp())
    end = int(datetime.combine(date_to + timedelta(days=1), dtime.min, tzinfo=LOCAL_TZ).timestamp())

    # --- build query (same time rule as the rest of the API) ---
    select_columns = BASE_READING_FIELDS + [ALLOWED_READING_FIELDS[f] for f in requested]
    sql = (
        f"SELECT {', '.join(select_columns)}, {READING_TIME} AS readingTime "
        f"FROM reading WHERE {READING_TIME} >= ? AND {READING_TIME} < ?"
    )
    params: list = [start, end]
    if node_ids:
        sql += f" AND leafNodeID IN ({', '.join('?' for _ in node_ids)})"
        params.extend(node_ids)
    sql += f" ORDER BY {READING_TIME} ASC, readingID ASC"

    # output column order: identity, readable time, raw times, then the picked fields
    columns = ["readingID", "leafNodeID", "time", "realTimeLog", "receivedAt"] + [
        ALLOWED_READING_FIELDS[f] for f in requested
    ]

    def rows():
        with closing(db_connect()) as conn:
            for r in conn.execute(sql, params):
                d = dict(r)
                d["time"] = local_time_text(d.pop("readingTime"))
                yield {c: d[c] for c in columns}

    filename = f"vitisense_{date_from.isoformat()}_to_{date_to.isoformat()}.{file_format}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}

    if file_format == "csv":
        def csv_stream():
            # streamed row by row, so a long date range never sits in memory
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=columns)
            writer.writeheader()
            for row in rows():
                writer.writerow(row)  # None -> empty cell
                if buf.tell() > 64_000:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue()

        return StreamingResponse(csv_stream(), media_type="text/csv", headers=headers)

    readings = list(rows())
    return JSONResponse(
        {
            "count": len(readings),
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
            "fields": requested,
            "readings": readings,
        },
        headers=headers,
    )


@app.on_event("startup")
def startup() -> None:
    init_db()
    if SERIAL_ENABLED:
        threading.Thread(target=serial_reader, daemon=True).start()
    else:
        print("ingest: serial reader disabled (SERIAL_ENABLED=0), serving the API only", flush=True)


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
def history(hours: float = Query(24, gt=0, le=504), leafNodeID: int | None = None) -> dict:
    """
    Readings from the last `hours`, oldest first. Add leafNodeID to get just one node's.

    GET /api/history?hours=168&leafNodeID=2
    """
    since = int(time.time() - hours * 3600)
    sql = f"SELECT * FROM reading WHERE {READING_TIME} >= ?"
    args = [since]
    if leafNodeID is not None:
        sql += " AND leafNodeID = ?"
        args.append(leafNodeID)
    sql += f" ORDER BY {READING_TIME} ASC, readingID ASC"
    with db_connect() as conn:
        rows = conn.execute(sql, args).fetchall()
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