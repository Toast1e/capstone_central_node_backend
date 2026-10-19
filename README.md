# Central node backend

The Raspberry Pi side of the mildew station. It reads the readings the
central node receives from the leaf nodes, stores them in a database, and
serves them, together with the VitiSense app, on the Pi's own Wi-Fi hotspot.

For a non-technical walk through the whole system, from sensor to app, see
[SYSTEM_GUIDE.md](SYSTEM_GUIDE.md).

```
[leaf node]                 [central node]                [Raspberry Pi 4B]
SHT31 + DS3231              Heltec on USB                 Docker
  │  LoRa 868 MHz  reading ►  │  JSON lines @115200         ├─ ingest: serial → SQLite → API :8000
  └───────────────◄ Ack ──────└────────────────────────────►└─ web: NGINX :80/:443 → app + /api proxy
```

## Where it fits

| Project                                                           | Runs on                         | Job                                                    |
| ----------------------------------------------------------------- | ------------------------------- | ------------------------------------------------------ |
| [leaf_node_LoRa](https://github.com/Toast1e/leaf_node_LoRa)       | each leaf node                  | measure, log to SD, send by LoRa                       |
| [central_node_LoRa](https://github.com/Toast1e/central_node_LoRa) | the Heltec on the Pi's USB port | receive, acknowledge, print each reading on USB serial |
| **capstone_central_node_backend** (this repo)                     | the Pi                          | store the readings, serve the API and the app          |
| [central_node_pwa](https://github.com/Toast1e/central_node_pwa)   | phones, served from the Pi      | the VitiSense app                                      |

## What is in this repo

| Path                                 | What it is                                                                                 |
| ------------------------------------ | ------------------------------------------------------------------------------------------ |
| `ingest/app.py`                      | one Python program: reads the central node's serial output into SQLite, and serves the API |
| `ingest/test_ingest.py`              | checks `app.py` against the central node's output, without a board                         |
| `web/`                               | the NGINX container: builds and serves the app, and forwards `/api/` to `ingest`           |
| `certs/`                             | where the HTTPS certificate goes (see [certs/README.md](certs/README.md))                  |
| `docker-compose.yml`, `.env.example` | how the two containers are started, and the one setting you must fill in                   |
| `firmware/receiver/`                 | an earlier receiver firmware. **Superseded by central_node_LoRa, do not flash it**         |

## How a reading gets here

1. A leaf node takes a reading and sends it by LoRa.
2. The central node answers with an acknowledgement (_Ack_) and prints the
   reading on USB serial as one line of JSON:

   ```
   {"leafNodeID":5,"readingID":70000,"realTimeLog":1790940463,"temperature":23.46,"humidity":100,"windSpeed":2,"leafWetness":12,"status":7,"rssi":-87.5,"snr":9.25}
   ```

3. `ingest` reads the line, adds the time it arrived, and stores it.

The ingest stores every line that is a JSON object with a `leafNodeID`, keeps
the latest `{"gateway": ...}` heartbeat line for the status page, and only
logs anything else.

A leaf node that gets no Ack keeps the reading and sends it again later. So a
reading can arrive hours late, and now and then twice. The ingest stores each
reading once and the API sorts by the time a reading was taken, not the time
it arrived. The leaf node's SD card remains the complete record; this
database is the live view.

## What is stored

Each reading becomes one row of the `reading` table:

| Column                     | Comes from     | Meaning                                                                                                             |
| -------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------- |
| `readingID`                | the database   | row number. **Not** the leaf node's own reading number                                                              |
| `leafNodeID`               | the line       | which leaf node took the reading                                                                                    |
| `realTimeLog`              | the line       | when the reading was taken: unix seconds, UTC, from the leaf node's clock. Empty if that clock could not be trusted |
| `temperature`, `humidity`  | the line       | °C and %. Empty if the sensor failed, never `0`                                                                     |
| `windSpeed`, `leafWetness` | the line       | placeholder values until the leaf node has those sensors                                                            |
| `receivedAt`               | the Pi's clock | when the line arrived: unix seconds, UTC                                                                            |
| `sentAt`                   | the line       | set to `realTimeLog`: a reading is first sent as soon as it is taken                                                |
| `resentAt`                 | the line       | only for a reading the leaf node sent again from its queue: when it was sent again                                  |
| `isSent`, `isReceived`     | the ingest     | always `1` for a stored reading                                                                                     |

A leaf node is added to the `leafnode` table by its first reading, under the
ID it transmits and the name `leaf-<ID>`; `lastConnectedAt` is updated by
every reading after that.

The line's `readingID` (the leaf node's own count), `status`, `rssi` and
`snr` are not stored in the table. The last value of each, per leaf node, is
shown by `/api/status`. `status` is a sum: 1 sensor ok, 2 clock ok, 4 saved
to the leaf node's SD card, 8 sent again from the queue; `7` is a fully
healthy reading.

Two rows with the same `leafNodeID` and `realTimeLog` are the same reading,
so the second is dropped.

## Setting it up

The seven steps below take a new Pi and a flashed central node to a working
app.

### 1. Flash the central node

The receiver is the **central_node_LoRa** project (its own repo). Open it in
PlatformIO and upload it to the Heltec that will sit on the Pi's USB port.
Its radio settings (`lib/projectDefinitions`) and packet format
(`lib/packetHandler/packet.*`) are kept identical to the leaf node's.

Sanity check before touching the Pi: open the serial monitor at 115200. Within
5 minutes (the leaf node's interval) you should see, per packet, a
`Receive: ...` line followed by one JSON line like the one above, and once a
minute a heartbeat:

```
{"gateway":"central-node","uptime_s":600,"rx_ok":2,"rx_duplicate":0,"rx_bad":0}
```

The onboard LED also blinks once per received packet.

`firmware/receiver/` in this repo is the earlier receiver. It does not
acknowledge readings and uses the old radio settings and packet format, so it
does not work with the current leaf nodes. Do not flash it.

### 2. Prepare the Pi (once)

SSH in, then:

```bash
# correct local time on the charts
sudo timedatectl set-timezone Africa/Johannesburg

# install Docker + the compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# log out and back in so the group change takes effect
```

Verify: `docker --version` and `docker compose version` both answer.

#### Wi-Fi hotspot

Phones join the Pi's own Wi-Fi to use the app. Raspberry Pi OS's
NetworkManager can run the hotspot, and hand out addresses to phones, by
itself. **Do this over Ethernet (or with a keyboard and screen).** The Pi
leaves its current Wi-Fi network when the hotspot starts.

```bash
sudo raspi-config    # Localisation Options → WLAN Country (the hotspot won't start without it)
sudo nmcli device wifi hotspot ifname wlan0 con-name CentralNode ssid CentralNode password "<8+ characters>"
sudo nmcli connection modify CentralNode connection.autoconnect yes connection.autoconnect-priority 100
```

On the hotspot the Pi is `10.42.0.1`. From now on use Ethernet whenever the
Pi needs internet (updates, building the images, renewing the certificate).

### 3. Get the code onto the Pi

The `web` container builds the app from `../central_node_pwa`, so clone
both repos into the same folder:

```bash
mkdir -p ~/central-node && cd ~/central-node
git clone https://github.com/Toast1e/capstone_central_node_backend.git
git clone https://github.com/Toast1e/central_node_pwa.git
```

The app is built from whatever is checked out in `central_node_pwa`, so
switch branches there if you're deploying one that isn't merged yet.

### 4. Point it at the central node's serial port

Plug the central node into the Pi, then:

```bash
cd ~/central-node/capstone_central_node_backend
ls -l /dev/serial/by-id/
```

You'll see something like
`usb-Espressif_USB_JTAG_serial_debug_unit_...` (the WiFi LoRa 32 V4, which
has USB built into its processor) or `usb-Silicon_Labs_CP2102_...` (boards
with a separate USB chip). Put that full path in the env file:

```bash
cp .env.example .env
nano .env      # SERIAL_PORT=/dev/serial/by-id/usb-...-if00
```

### 5. Build and start

```bash
docker compose up -d --build
```

The first build downloads the app's npm packages and fonts, so the Pi needs
internet. It takes several minutes on a Pi 4. Then:

```bash
docker compose logs -f ingest
```

You should see `ingest: listening on /dev/lora @ 115200`, the central node's
other lines tagged `[gw]`, and within 5 minutes
`ingest: stored node=... reading=... t=... rh=... rssi=...`.

`docker compose logs web` shows whether it's serving HTTP or HTTPS (next
step).

Only one program can read the serial port at a time: close any serial
monitor that is open on the central node before starting the containers.

### 6. HTTPS (needed to install the app)

Phones only install the app to the home screen, and only run its offline
cache, when it's served over HTTPS with a certificate they trust. Put the
certificate in `certs/` as `fullchain.pem` + `privkey.pem`, then run
`docker compose restart web` (see [certs/README.md](certs/README.md)). There
are two ways to get one:

- **Own domain + Let's Encrypt (for real users).** Pick a name such as
  `vitisense.example.com`. Get a certificate with certbot's **DNS-01**
  challenge, which works even though the Pi isn't reachable from the
  internet. certbot keeps it in `/etc/letsencrypt/live/<name>/` as symlinks,
  so copy the files with `cp -L`. It lasts 90 days; renew it while the Pi
  has internet. Then point the name at the Pi, both on the hotspot and in
  public DNS. The public record is for Android, which sometimes looks names
  up over mobile data instead of the hotspot:

  ```bash
  echo "address=/vitisense.example.com/10.42.0.1" | sudo tee /etc/NetworkManager/dnsmasq-shared.d/vitisense.conf
  sudo nmcli connection up CentralNode
  # and at your DNS provider: A record  vitisense.example.com → 10.42.0.1
  ```

- **mkcert (demos on your own phones).** On your computer:
  `mkcert -install`, then
  `mkcert -cert-file certs/fullchain.pem -key-file certs/privkey.pem 10.42.0.1`.
  Copy the two files to the Pi's `certs/`. Every phone must install and
  trust mkcert's root certificate (`mkcert -CAROOT` shows where it is) by
  hand. On iPhone, also switch it on under Settings → General → About →
  Certificate Trust Settings.

### 7. Open and install the app

Join the `CentralNode` Wi-Fi, then open the app in the browser:

- `https://vitisense.example.com/` (your name) or `https://10.42.0.1/` (mkcert)
- `http://10.42.0.1/` while there's no certificate

To put the app icon on the home screen (HTTPS only):

- **Android (Chrome):** ⋮ menu → _Install app_
- **iPhone (Safari):** Share → _Add to Home Screen_

Without HTTPS, _Add to Home Screen_ still makes an icon, but it opens as an
ordinary browser tab.

## API

| Endpoint                             | Returns                                                                                    |
| ------------------------------------ | ------------------------------------------------------------------------------------------ |
| `/api/leafnodes`                     | every leaf node with its 50 latest readings (`?ids=1,2` for just those nodes)              |
| `/api/leafnodes/status`              | every leaf node's status and connection quality (`GOOD`, `LOW`, `NONE`), no readings       |
| `/api/leafnodes/{id}/details`        | one leaf node's newest reading, signal and location (404 if it doesn't exist)              |
| `PATCH /api/leafnodes/{id}/location` | set a leaf node's `blockID`, `row`, `latitude` or `longitude`; only the fields sent change |
| `/api/alerts`                        | alerts that have not been acknowledged                                                     |
| `/api/alerts/acknowledged`           | alerts that have been acknowledged                                                         |
| `PATCH /api/alerts/{id}/acknowledge` | mark an alert acknowledged and set `resolvedAt` (404 if it doesn't exist)                  |
| `/api/centralnode`                   | the central node's name, uptime, free storage and serial state                             |
| `PATCH /api/centralnode`             | rename the central node (max 50 characters)                                                |
| `/api/export/readings`               | readings as a CSV or JSON download, between `from` and `to` (both required)                |

`/api/latest` returns one row of the `reading` table:

```json
{
  "readingID": 812,
  "realTimeLog": 1790940463,
  "temperature": 23.46,
  "humidity": 100.0,
  "windSpeed": 2.0,
  "leafWetness": 12.0,
  "isSent": 1,
  "isReceived": 1,
  "sentAt": 1790940463,
  "receivedAt": 1790940465,
  "resentAt": null,
  "leafNodeID": 5
}
```

`/api/history` returns `{"hours":24.0,"count":288,"reading":[ ...rows... ]}`.

`/api/status`:

```json
{
  "serial_connected": true,
  "serial_port": "/dev/lora",
  "last_packet_at": 1790940465,
  "packets_stored": 812,
  "duplicates_dropped": 3,
  "nodes": {
    "5": {
      "last_packet_at": 1790940465,
      "readingID": 70000,
      "status": 7,
      "rssi": -87.5,
      "snr": 9.25
    }
  },
  "gateway_heartbeat": {
    "gateway": "central-node",
    "uptime_s": 600,
    "rx_ok": 2,
    "rx_duplicate": 0,
    "rx_bad": 0
  },
  "started_at": 1790900000,
  "server_time": 1790940470,
  "last_packet_age_s": 5
}
```

| Field                                  | Meaning                                                                                                         |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `serial_connected`                     | the ingest has the central node's serial port open                                                              |
| `last_packet_age_s`                    | seconds since any reading arrived. Leaf nodes send every 5 minutes                                              |
| `packets_stored`, `duplicates_dropped` | counts since the ingest started                                                                                 |
| `nodes`                                | per leaf node: when it was last heard, its own reading number, its `status`, and the signal quality             |
| `gateway_heartbeat`                    | the central node's last once-a-minute heartbeat line. If `uptime_s` stops growing, the central node has stopped |

These numbers are kept in memory and start again from zero when the ingest
container restarts. The readings themselves are in the database.

On its `main` branch at the time of writing, the app shows built-in sample
readings and does not call this API yet. The API's field names are the ones
the app's screens already use (`temperature`, `humidity`, `leafWetness`,
`receivedAt`).

The database is a named Docker volume (`weather-data`). Back it up with:

```bash
docker compose cp ingest:/data/weather.db ./weather-backup.db
```

## Testing the ingest without a board

`ingest/test_ingest.py` starts the ingest against a pretend serial port and
feeds it the same lines the central node prints: readings, a repeat, a
reading that arrives late, a failed sensor, a leaf node with no clock, a
heartbeat and junk. It then checks what was stored and what the API returns.

```bash
python3 -m venv ~/ingest-venv && . ~/ingest-venv/bin/activate
pip install -r ingest/requirements.txt
python ingest/test_ingest.py        # ends with: ingest: all checks passed
```

Run it after changing `app.py`, or after changing the line the central node
prints (`Packet::toRecordJson` in central_node_LoRa).

## Troubleshooting

- **`ingest: serial error` in the logs** — wrong `SERIAL_PORT` in `.env`,
  the central node isn't plugged in, or a serial monitor has the port open.
  Re-check `ls -l /dev/serial/by-id/`, then `docker compose up -d` again
  (compose re-reads `.env`).
- **Heartbeats arrive (`gateway_heartbeat` in `/api/status`) but never a
  reading** — the central node is running but hears no leaf node. Usually a
  radio settings mismatch: compare
  `lib/projectDefinitions/projectDefinitions.h` in the leaf and central node
  projects; every `LORA_*` value (freq, BW, SF, CR, sync word, preamble) must
  be identical. See the troubleshooting section of central_node_LoRa's README.
- **Readings on serial but nothing in the app** — check
  `docker compose logs ingest` for `bad JSON dropped`, `DATABASE ERROR` or
  `DATABASE IS FROM AN OLDER VERSION` lines, and
  `curl http://localhost/api/status` on the Pi. A database made by an older
  version has different columns: start over with a clean one (below).
- **A reading shows up hours late, out of order in the logs** — normal: the
  leaf node could not reach the central node and sent it later. The API
  returns it at the time it was taken.
- **Times look 2 hours behind** — all times are UTC, not South African time.
- **Changed a file?** Rebuild the affected container:
  `docker compose up -d --build`.
- **App changes don't show up** — the app is built into the `web` image.
  Pull the PWA repo (`git -C ../central_node_pwa pull`), then
  `docker compose up -d --build web`.
- **Start over with a clean database** (this deletes every stored reading;
  back it up first, see [API](#api)):
  `docker compose down -v && docker compose up -d`.

## Notes on time

Each reading carries two times, both unix seconds in UTC. `realTimeLog` is
when the leaf node took it (its DS3231, set when the node is flashed) and is
what the API sorts by. `receivedAt` is when the Pi got it. They differ for a
reading that was queued on the leaf node and sent later (`resentAt` is then
set). A leaf node whose clock cannot be trusted sends no `realTimeLog`; that
reading is placed by `receivedAt`, so the Pi's clock still matters.

On the hotspot the Pi usually has no internet, so NTP can't set its clock,
and the Pi 4 has no battery-backed clock. After a power cut it can be
wrong. A DS3231 RTC module on the Pi's I²C pins keeps it right; enable it
with `dtoverlay=i2c-rtc,ds3231` in `/boot/firmware/config.txt`.
