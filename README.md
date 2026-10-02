# Mildew station dashboard

LoRa leaf nodes → central node (Heltec) on the Pi's USB port → Docker
(ingest + NGINX) → the VitiSense PWA, served on the Pi's own Wi-Fi hotspot.

```
[leaf node]                 [central node]                [Raspberry Pi 4B]
SHT31 + DS3231              Heltec on USB                 Docker
  │  LoRa 868 MHz  reading ►  │  NDJSON @115200             ├─ ingest: serial → SQLite → API :8000
  └───────────────◄ Ack ──────└────────────────────────────►└─ web: NGINX :80/:443 → PWA + /api proxy
```

Three projects have to agree with each other:

| Project | Runs on | Talks to |
|---------|---------|----------|
| [leaf_node_LoRa](https://github.com/Toast1e/leaf_node_LoRa) | each leaf node | the central node, by LoRa |
| [central_node_LoRa](https://github.com/Toast1e/central_node_LoRa) | the Heltec on the Pi's USB port | leaf nodes by LoRa, this repo's ingest by USB serial |
| this repo | the Pi | the central node by USB serial, the app by HTTP |

The central node acknowledges every reading and prints each new one as one
JSON line, whose keys are the `reading` table's column names:

```
{"leafNodeID":5,"readingID":70000,"realTimeLog":1790940463,"temperature":23.46,"humidity":100,"windSpeed":2,"leafWetness":12,"status":7,"rssi":-87.5,"snr":9.25}
```

A leaf node that gets no acknowledgement keeps the reading and sends it again
later, so readings can arrive late and, rarely, twice; the ingest stores each
one once. The leaf node's SD card remains the record of truth; the app is the
live view.

The app itself lives in its own repo,
[central_node_pwa](https://github.com/Toast1e/central_node_pwa). The `web`
container builds it from a checkout next to this folder and serves it
together with the API.

## 1. Flash the central node

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

## 2. Prepare the Pi (once)

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

### Wi-Fi hotspot

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

## 3. Get the code onto the Pi

The `web` container builds the app from `../central_node_pwa`, so clone
both repos into the same folder:

```bash
mkdir -p ~/central-node && cd ~/central-node
git clone https://github.com/Toast1e/capstone_central_node_backend.git
git clone https://github.com/Toast1e/central_node_pwa.git
```

The app is built from whatever is checked out in `central_node_pwa`, so
switch branches there if you're deploying one that isn't merged yet.

## 4. Point it at the receiver's serial port

Plug the receiver into the Pi, then:

```bash
cd ~/central-node/capstone_central_node_backend
ls -l /dev/serial/by-id/
```

You'll see something like
`usb-Espressif_USB_JTAG_serial_debug_unit_...` (native-USB boards) or
`usb-Silicon_Labs_CP2102_...` (Wireless Stick V3). Put that full path in the
env file:

```bash
cp .env.example .env
nano .env      # SERIAL_PORT=/dev/serial/by-id/usb-...-if00
```

## 5. Build and start

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

## 6. HTTPS (needed to install the app)

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

## 7. Open and install the app

Join the `CentralNode` Wi-Fi, then open the app in the browser:

- `https://vitisense.example.com/` (your name) or `https://10.42.0.1/` (mkcert)
- `http://10.42.0.1/` while there's no certificate

To put the app icon on the home screen (HTTPS only):

- **Android (Chrome):** ⋮ menu → *Install app*
- **iPhone (Safari):** Share → *Add to Home Screen*

Without HTTPS, *Add to Home Screen* still makes an icon, but it opens as an
ordinary browser tab.

## API (if you want the raw data)

| Endpoint                  | Returns                                              |
|---------------------------|------------------------------------------------------|
| `/api/latest`             | most recent reading                                  |
| `/api/history?hours=24`   | readings for a window (max 504 h = 21 days)          |
| `/api/status`             | serial state, last-packet age, counts, last packet per leaf node |

The database is a named Docker volume (`weather-data`). Back it up with:

```bash
docker compose cp ingest:/data/weather.db ./weather-backup.db
```

## Troubleshooting

- **`ingest: serial error` in the logs** — wrong `SERIAL_PORT` in `.env`, or
  the receiver isn't plugged in. Re-check `ls -l /dev/serial/by-id/`, then
  `docker compose up -d` again (compose re-reads `.env`).
- **Central node prints heartbeats but never a reading** — radio parameter
  mismatch. Compare `lib/projectDefinitions/projectDefinitions.h` in the leaf
  and central node projects; every `LORA_*` value (freq, BW, SF, CR, sync
  word, preamble) must be identical.
- **Readings on serial but nothing in the app** — check
  `docker compose logs ingest` for `bad JSON dropped`, `DATABASE ERROR` or
  `DATABASE IS FROM AN OLDER VERSION` lines, and
  `curl http://localhost/api/status` on the Pi. A database made by an older
  version has different columns: start over with a clean one (below).
- **Check the ingest without a board** — `python ingest/test_ingest.py` feeds
  it the central node's lines through a pseudo-terminal.
- **Changed a file?** Rebuild the affected container:
  `docker compose up -d --build`.
- **App changes don't show up** — the app is built into the `web` image.
  Pull the PWA repo (`git -C ../central_node_pwa pull`), then
  `docker compose up -d --build web`.
- **Start over with a clean database**:
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
