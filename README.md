# Mildew station dashboard

LoRa weather station → Heltec receiver on the Pi's USB port → Docker
(ingest + NGINX) → local website.

```
[field node]                [ground station]              [Raspberry Pi 4B]
SHT31 + DS3231              Heltec on USB                 Docker
  │  LoRa 868 MHz             │  NDJSON @115200             ├─ ingest: serial → SQLite → API :8000
  └──────────────────────────►└────────────────────────────►└─ web: NGINX :80 → dashboard + /api proxy
```

The sender firmware is unchanged — its JSON payload is already exactly what
the gateway forwards. The SD card remains the record of truth; the dashboard
is the live view.

## 1. Flash the receiver

`firmware/receiver/` is a PlatformIO project. Open it, check two things
against your hardware, then upload:

- `LORA_FREQ` — 868.0 must match your board's band variant (it matches the
  sender, so if the sender transmits, this is right).
- `LORA_TCXO` — 1.8 V is correct for V3.2+/V4 boards. If `radio.begin()`
  reports error `-706`/`-707` on an older board, set it to `0`.

All other radio parameters are copied from the sender and must stay identical.

Sanity check before touching the Pi: open the serial monitor at 115200. Within
5 minutes (the sender's log interval) you should see one JSON line per packet:

```
{"node":"mildew-1","seq":12,"ts":1753693500,"temp_c":18.42,...,"rssi":-87.5,"snr":9.25}
```

The onboard LED also blinks once per received packet.

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

## 3. Copy this project to the Pi

From your computer:

```bash
scp -r mildew-dashboard pi@<pi-ip>:~/
```

## 4. Point it at the receiver's serial port

Plug the receiver into the Pi, then:

```bash
cd ~/mildew-dashboard
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

First build takes a few minutes on a Pi 4. Then:

```bash
docker compose logs -f ingest
```

You should see `ingest: listening on /dev/lora @ 115200`, the gateway's
boot lines tagged `[gw]`, and within 5 minutes
`ingest: stored seq=... t=... rssi=...`.

## 6. Open the dashboard

From any device on the same network:

- `http://<pi-ip>/`
- or `http://raspberrypi.local/` (works from most laptops/phones via mDNS)

The page refreshes itself every minute. The link dot goes amber when no
packet has arrived for ~11 min (two missed slots) and red after ~30 min.

## API (if you want the raw data)

| Endpoint                  | Returns                                              |
|---------------------------|------------------------------------------------------|
| `/api/latest`             | most recent reading                                  |
| `/api/history?hours=24`   | readings for a window (max 504 h = 21 days)          |
| `/api/status`             | serial state, last-packet age, counts, seq-gap misses|

The database is a named Docker volume (`weather-data`). Back it up with:

```bash
docker compose cp ingest:/data/weather.db ./weather-backup.db
```

## Troubleshooting

- **`ingest: serial error` in the logs** — wrong `SERIAL_PORT` in `.env`, or
  the receiver isn't plugged in. Re-check `ls -l /dev/serial/by-id/`, then
  `docker compose up -d` again (compose re-reads `.env`).
- **Receiver prints heartbeats but never a reading** — radio parameter
  mismatch. Diff the `#define LORA_*` block against the sender; every value
  (freq, BW, SF, CR, sync word, preamble) must be identical.
- **Readings on serial but nothing in the dashboard** — check
  `docker compose logs ingest` for `bad JSON dropped` lines, and
  `curl http://localhost/api/status` on the Pi.
- **Changed a file?** Rebuild the affected container:
  `docker compose up -d --build`.
- **Start over with a clean database**:
  `docker compose down -v && docker compose up -d`.

## Notes on time

The dashboard charts by the Pi's clock (`received_at`, set by NTP), which is
robust even if the field node's RTC drifts. The sender's own `ts` is stored
alongside for cross-checking against the SD card CSVs.
