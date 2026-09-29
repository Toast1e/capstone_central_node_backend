/* ============================================================================
   Mildew ground station — LoRa → USB serial gateway  |  Heltec ESP32-S3 board
   (WiFi LoRa 32 V3/V4, Wireless Stick V3/V4 — all share the same SX1262 pins)

   Job: sit on the Pi's USB port, listen on the exact same LoRa parameters as
   the field node, and print ONE JSON LINE per received packet on the serial
   port at 115200 baud. The Pi-side ingest container reads those lines.

   Output format (newline-delimited JSON, "NDJSON"):
     - A reading, i.e. the sender's payload with radio metadata appended:
         {"node":"mildew-1","seq":42,"ts":1753693500,"temp_c":18.42,
          "humidity_pct":76.1,"dewpoint_c":14.21,"battery_v":3.912,
          "battery_pct":66,"sd":true,"rssi":-87.5,"snr":9.25}
     - A gateway heartbeat once a minute:
         {"gateway":"mildew-gw","uptime_s":600,"rx_ok":2,"crc_err":0}
     - Anything that is not valid JSON (boot messages, diagnostics) is ignored
       by the ingest service, so plain Serial.println debugging is safe here.

   The radio parameters below MUST be byte-for-byte identical to the sender's,
   or you get silence with no error to explain it.
   ========================================================================== */

#include <SPI.h>
#include <RadioLib.h>

// ---- SX1262 (onboard, default SPI bus — same wiring on V3 and V4 boards) ----
#define LORA_NSS   8
#define LORA_SCK   9
#define LORA_MOSI  10
#define LORA_MISO  11
#define LORA_RST   12
#define LORA_BUSY  13
#define LORA_DIO1  14

// ---- must match the sender exactly ----
#define GW_ID          "mildew-gw"
#define LORA_FREQ      868.0   // MHz
#define LORA_BW        125.0   // kHz
#define LORA_SF        9
#define LORA_CR        7       // coding rate 4/7
#define LORA_SYNC      0x34    // private network
#define LORA_PREAMBLE  8
#define LORA_TCXO      1.8     // V — Heltec V3.2+/V4 feed the TCXO from DIO3
                               // (if radio.begin() returns -706/-707 on an
                               //  older V3.0/V3.1 board, change this to 0)

#define LED_PIN    35          // onboard white LED — blinks once per packet
#define STATUS_EVERY_S 60      // heartbeat interval

SX1262 radio = new Module(LORA_NSS, LORA_DIO1, LORA_RST, LORA_BUSY);

volatile bool rxFlag = false;
uint32_t rxOk = 0, crcErr = 0, otherErr = 0;
uint32_t lastStatus = 0;

// ISR — keep it to a flag; all real work happens in loop()
void IRAM_ATTR onRxDone(void) { rxFlag = true; }

bool initRadio() {
  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_NSS);
  int st = radio.begin(LORA_FREQ, LORA_BW, LORA_SF, LORA_CR, LORA_SYNC,
                       10 /*TX power, unused — we never transmit*/,
                       LORA_PREAMBLE, LORA_TCXO, false);
  if (st != RADIOLIB_ERR_NONE) {
    Serial.printf("GW: SX1262 init failed (%d) — check LORA_TCXO / band variant\n", st);
    return false;
  }
  radio.setDio1Action(onRxDone);
  st = radio.startReceive();
  if (st != RADIOLIB_ERR_NONE) {
    Serial.printf("GW: startReceive failed (%d)\n", st);
    return false;
  }
  return true;
}

void printStatus() {
  Serial.printf("{\"gateway\":\"%s\",\"uptime_s\":%lu,\"rx_ok\":%lu,\"crc_err\":%lu,\"rx_err\":%lu}\n",
                GW_ID, (unsigned long)(millis() / 1000UL),
                (unsigned long)rxOk, (unsigned long)crcErr, (unsigned long)otherErr);
}

void setup() {
  Serial.begin(115200);
  // wait up to 3 s for the USB host so boot messages aren't lost
  for (int i = 0; i < 30 && !Serial; i++) delay(100);
  delay(200);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  while (!initRadio()) {
    Serial.println("GW: retrying radio init in 5 s...");
    delay(5000);
  }
  Serial.printf("GW: listening — %.1f MHz SF%d BW%.0f sync 0x%02X\n",
                (double)LORA_FREQ, LORA_SF, (double)LORA_BW, LORA_SYNC);
  printStatus();
}

void loop() {
  if (rxFlag) {
    rxFlag = false;

    String payload;
    int st = radio.readData(payload);

    if (st == RADIOLIB_ERR_NONE) {
      float rssi = radio.getRSSI();
      float snr  = radio.getSNR();
      payload.trim();

      // Forward only well-formed JSON objects; splice the radio metadata in
      // before the closing brace so the Pi gets a single flat object.
      if (payload.startsWith("{") && payload.endsWith("}") && payload.length() > 2) {
        rxOk++;
        payload.remove(payload.length() - 1);   // drop trailing '}'
        Serial.printf("%s,\"rssi\":%.1f,\"snr\":%.2f}\n", payload.c_str(), rssi, snr);
        digitalWrite(LED_PIN, HIGH); delay(30); digitalWrite(LED_PIN, LOW);
      } else {
        otherErr++;
        Serial.println("GW: non-JSON payload dropped");  // foreign traffic on our sync word
      }
    } else if (st == RADIOLIB_ERR_CRC_MISMATCH) {
      crcErr++;
      Serial.println("GW: CRC error (packet corrupted in flight)");
    } else {
      otherErr++;
      Serial.printf("GW: readData failed (%d)\n", st);
    }

    // re-arm the receiver for the next packet
    radio.startReceive();
  }

  if (millis() - lastStatus >= STATUS_EVERY_S * 1000UL) {
    lastStatus = millis();
    printStatus();
  }

  delay(5);
}
