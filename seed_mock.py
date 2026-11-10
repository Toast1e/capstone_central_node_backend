"""
Fake data for local testing. NOT for the Pi. WIPES leaf nodes, readings and alerts first.

  docker compose -f docker-compose.yml -f docker-compose.local.yml exec -T ingest python - < seed_mock.py

6 leaf nodes, a week of readings every 15 min, through store_reading().
  1,2,4 GOOD   3 WEAK SIGNAL   5 NO SIGNAL (silent 6 h, low battery)   6 GOOD but no location
Mildew spells (SPELLS) push some nodes into moderate/high risk; alerts from before the last 24 h are acknowledged.
"""
import math
import random
import time

import app  # the API module, already inside the container

app.init_db()
random.seed(42)

STEP = 15 * 60
DAYS = 7
now = int(time.time())
grid_end = now // STEP * STEP
NODES = [1, 2, 3, 4, 5, 6]

# (node, hours ago it starts, hours it lasts, risk) - risk: 1 moderate, 2 high
SPELLS = [
    (1, 5, 3, 2),
    (2, 30, 4, 2),
    (3, 8, 3, 1),
    (4, 50, 4, 2),
    (5, 10, 2, 1),
    (6, 12, 3, 1),
]
# (temperature, humidity, leaf wetness) that land in each risk band, with a little noise
SPELL_VALUES = {1: (21, 85, 50), 2: (20, 95, 75)}

def in_spell(node, t):
    for n, start_h, length_h, risk in SPELLS:
        start = grid_end - start_h * 3600
        if n == node and start <= t < start + length_h * 3600:
            return risk
    return None

conn = app.db_connect()

# wipe the old data (the central node row stays)
for table in ("leafnode_alert", "alert", "reading", "leafnode"):
    conn.execute(f"DELETE FROM {table}")
conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('leafnode_alert','alert','reading','leafnode')")
conn.commit()

battery = {n: {5: 34}.get(n, random.randint(76, 95)) for n in NODES}
rssi = {n: {3: -116}.get(n, random.uniform(-95, -75)) for n in NODES}


def ingest(keep):
    """Store every reading whose time passes keep(t)."""
    count = 0
    for node in NODES:
        # node 5 went quiet 6 hours ago; the others reach right up to now
        last_slot = grid_end - (6 * 3600 if node == 5 else 0)
        for t in range(last_slot - DAYS * 86400, last_slot + 1, STEP):
            if not keep(t):
                continue
            hour = (t % 86400) / 3600
            day_wave = math.sin((hour - 9) / 24 * 2 * math.pi)      # warmest mid-afternoon
            pkt = {
                "leafNodeID": node,
                "realTimeLog": t,
                "temperature": round(19 + 7 * day_wave + random.uniform(-1, 1), 2),
                "humidity": round(min(100, max(35, (90 if node == 2 else 72) - 22 * day_wave + random.uniform(-4, 4))), 1),
                "windSpeed": round(max(0, 4 + 3 * random.random() + (2 if node == 2 else 0)), 1),
                "leafWetness": round(min(100, max(0, 45 - 35 * day_wave + random.uniform(-8, 8))), 1),
                "battery": battery[node],
                "rssi": round(rssi[node] + random.uniform(-2, 2), 1),
                "snr": round(random.uniform(4, 10), 2),
            }
            risk = in_spell(node, t)
            if risk:                           # mildew spell: favourable conditions
                temp, hum, wet = SPELL_VALUES[risk]
                pkt["temperature"] = round(temp + random.uniform(-1, 1), 2)
                pkt["humidity"] = round(hum + random.uniform(-2, 2), 1)
                pkt["leafWetness"] = round(wet + random.uniform(-4, 4), 1)
            elif random.random() < 0.02:       # the odd value the node couldn't measure
                del pkt["humidity"]
            count += app.store_reading(conn, pkt)
    return count


# Older than a day: store, then acknowledge everything raised so far. Open alerts would
# otherwise cover the later ones (an open alert blocks new alerts of the same or lower risk).
split = now - 86400
stored = ingest(lambda t: t < split)
conn.execute("UPDATE alert SET isAcknowledged = 1, resolvedAt = raisedAt + 3600")
conn.commit()
# Last 24 h: alerts raised here stay open
stored += ingest(lambda t: t >= split)

# store_reading stamps every node as "heard just now" - rewind the silent one
conn.execute("UPDATE leafnode SET lastConnectedAt = ?, leafNodeStatus = 0 WHERE leafNodeID = 5",
             (grid_end - 6 * 3600,))
# where each node is (node 6 left unset on purpose)
for node, block, row in [(1, 1, 2), (2, 1, 5), (3, 2, 1), (4, 2, 4), (5, 2, 8)]:
    conn.execute("UPDATE leafnode SET blockID = ?, leafNodeRow = ? WHERE leafNodeID = ?", (block, row, node))
conn.commit()

active = conn.execute("SELECT COUNT(*) FROM alert WHERE isAcknowledged = 0").fetchone()[0]
done = conn.execute("SELECT COUNT(*) FROM alert WHERE isAcknowledged = 1").fetchone()[0]
print(f"stored {stored} new readings for {len(NODES)} nodes; {active} active and {done} acknowledged alerts")