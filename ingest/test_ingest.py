"""
Checks the ingest service against the exact lines the central node prints,
without a board: a pseudo-terminal stands in for the USB serial port.

Run from this folder (macOS / Linux / the Pi):
    pip install -r requirements.txt
    python test_ingest.py
"""

import os
import pty
import sqlite3
import tempfile
import threading
import time

master_fd, slave_fd = pty.openpty()
workdir = tempfile.mkdtemp()
os.environ["SERIAL_PORT"] = os.ttyname(slave_fd)
os.environ["DB_PATH"] = os.path.join(workdir, "weather.db")

import app  # noqa: E402  (must come after the environment is set)


def send(line: str) -> None:
    os.write(master_fd, (line + "\r\n").encode())


def wait_for(condition, what: str, timeout: float = 10) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for: {what}")


def packets() -> int:
    with app.status_lock:
        return app.link["packets_stored"] + app.link["duplicates_dropped"]


NOW = int(time.time())
T1 = NOW - 600      # two readings taken 5 minutes apart ...
T2 = NOW - 300
T_OLD = NOW - 3600  # ... and one from an hour ago that only gets through later

# The reading line is what central_node_LoRa prints (Packet::toRecordJson); the
# leaf node's unit test test_record_for_the_pi_uses_database_names pins its text.
READING_1 = ('{"leafNodeID":5,"readingID":70000,"realTimeLog":%d,"temperature":23.46,'
             '"humidity":100,"windSpeed":2,"leafWetness":12,"status":7,"rssi":-87.5,"snr":9.25}' % T1)
READING_2 = ('{"leafNodeID":5,"readingID":70001,"realTimeLog":%d,"temperature":24.01,'
             '"humidity":98.5,"windSpeed":2,"leafWetness":12,"status":7,"rssi":-88,"snr":9}' % T2)
RESENT = ('{"leafNodeID":5,"readingID":69990,"realTimeLog":%d,"temperature":19.5,'
          '"humidity":80,"windSpeed":2,"leafWetness":12,"status":15,"resentAt":%d,"rssi":-90,"snr":8}'
          % (T_OLD, T2))
SENSOR_FAILED = ('{"leafNodeID":6,"readingID":3,"realTimeLog":%d,"leafWetness":12,"status":6,'
                 '"rssi":-70,"snr":10}' % T2)
NO_CLOCK = '{"leafNodeID":7,"readingID":1,"temperature":20,"humidity":50,"leafWetness":0,"status":5,"rssi":-60,"snr":11}'

app.init_db()
threading.Thread(target=app.serial_reader, daemon=True).start()
wait_for(lambda: app.link["serial_connected"], "serial port opened")

# --- what the central node prints around a reading must not disturb anything
send("LoRa front end: GC1109 (V4.2)")
send("RX ready — listening")
send('Receive: "{\\"n\\":5,\\"i\\":70000}"  RSSI -87.5 dBm  SNR 9.25 dB')
send(READING_1)
wait_for(lambda: packets() == 1, "first reading stored")

send('{"gateway":"central-node","uptime_s":600,"rx_ok":1,"rx_duplicate":0,"rx_bad":0}')
send("{this is not json")
send("[1,2,3]")
send('{"hello":"world"}')

# --- the same reading again (the leaf missed the Ack): stored once
send(READING_1)
wait_for(lambda: packets() == 2, "duplicate handled")

send(READING_2)
send(RESENT)          # arrives last, was measured first
send(SENSOR_FAILED)
send(NO_CLOCK)
wait_for(lambda: packets() == 6, "all readings handled")

status = app.status()
assert status["packets_stored"] == 5, status
assert status["duplicates_dropped"] == 1, status
assert status["gateway_heartbeat"]["rx_ok"] == 1, status
assert status["nodes"][5]["readingID"] == 69990 and status["nodes"][5]["status"] == 15, status
assert set(status["nodes"]) == {5, 6, 7}, status

history = app.history(hours=24)
rows = history["reading"]
assert history["count"] == 5, history

# sorted by when the reading was TAKEN: the resent one comes first although it arrived late
node5 = [r for r in rows if r["leafNodeID"] == 5]
assert [r["realTimeLog"] for r in node5] == [T_OLD, T1, T2], node5
resent, first, second = node5
assert first["temperature"] == 23.46 and first["humidity"] == 100, first
assert first["windSpeed"] == 2 and first["leafWetness"] == 12, first
assert first["isSent"] == 1 and first["isReceived"] == 1, first
assert first["sentAt"] == T1 and first["resentAt"] is None, first
assert abs(first["receivedAt"] - NOW) < 60, first
assert resent["resentAt"] == T2, resent

# a failed sensor is stored as NULL, never as 0
failed = [r for r in rows if r["leafNodeID"] == 6][0]
assert failed["temperature"] is None and failed["humidity"] is None, failed

# no trusted clock on the leaf: no realTimeLog, placed by arrival time
no_clock = [r for r in rows if r["leafNodeID"] == 7][0]
assert no_clock["realTimeLog"] is None and abs(no_clock["receivedAt"] - NOW) < 60, no_clock

# "latest" is the newest measurement, not the old reading that arrived last
latest = app.latest()
assert latest["leafNodeID"] == 7 or latest["realTimeLog"] == T2, latest

# every node that sent something is registered under the ID it transmits
with sqlite3.connect(os.environ["DB_PATH"]) as conn:
    nodes = conn.execute("SELECT leafNodeID, leafNodeName, leafNodeStatus FROM leafnode ORDER BY 1").fetchall()
assert nodes == [(5, "leaf-5", 1), (6, "leaf-6", 1), (7, "leaf-7", 1)], nodes

# --- leaf node settings can be updated independently
renamed = app.rename_leaf_node(5, app.LeafNodeRename(name="  North block  "))
assert renamed == {"leafNodeID": 5, "leafNodeName": "North block"}, renamed
coordinates = app.update_leaf_node_coordinates(
    5, app.LeafNodeCoordinates(latitude=-33.92, longitude=18.42)
)
assert coordinates == {"leafNodeID": 5, "latitude": -33.92, "longitude": 18.42}, coordinates
with sqlite3.connect(os.environ["DB_PATH"]) as conn:
    node = conn.execute(
        "SELECT leafNodeName, leafNodeLatitude, leafNodeLongitude "
        "FROM leafnode WHERE leafNodeID = 5"
    ).fetchone()
assert node == ("North block", -33.92, 18.42), node
try:
    app.rename_leaf_node(5, app.LeafNodeRename(name="leaf-6"))
except app.HTTPException as exc:
    assert exc.status_code == 409, exc.status_code
else:
    raise AssertionError("duplicate leaf node name was accepted")

# --- a database error must not stop the reader: later readings still get stored
with sqlite3.connect(os.environ["DB_PATH"]) as conn:
    conn.execute("ALTER TABLE reading RENAME TO reading_hidden")
send(READING_2.replace(str(T2), str(T2 + 1)))
time.sleep(1.0)
with sqlite3.connect(os.environ["DB_PATH"]) as conn:
    conn.execute("ALTER TABLE reading_hidden RENAME TO reading")
send(READING_2.replace(str(T2), str(T2 + 2)))
wait_for(lambda: app.status()["packets_stored"] == 6, "reader survived a database error")

print("ingest: all checks passed")
