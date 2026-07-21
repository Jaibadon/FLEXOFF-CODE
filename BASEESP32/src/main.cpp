#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <string.h>
#include <stdio.h>

// ============================================================
//  Flex-Off BASE STATION (ESP32-C3, incl. SuperMini)
//
//  Receives telemetry and prints it. Also sends config to the nodes:
//  type commands in the serial monitor to tune them live.
//
//    env <ms> [id]     envelope window / group delay   (5..100 ms)
//    tap <1-127> [id]  LIS3DH tap threshold
//    tx  <hz> [id]     telemetry rate                  (10..200 Hz)
//    default [id]  restores default settings
//    (id: 0 = P1, 1 = P2, omit = both)
//
//  MUST match the transmitter on: channel + both packet layouts.
// ============================================================

constexpr uint8_t ESPNOW_CHANNEL = 11;
static const uint8_t BCAST[6] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};

// ===== shared protocol : KEEP BYTE-IDENTICAL WITH THE TRANSMITTER =====
constexpr uint8_t CONFIG_MAGIC = 0xC5;
constexpr uint8_t APPLY_ENV = 0x01, APPLY_TAP = 0x02, APPLY_TX = 0x04;
constexpr uint8_t TARGET_ALL = 0xFF;

typedef struct __attribute__((packed)) {       // base -> node (7 bytes)
  uint8_t  magic;
  uint8_t  target_id;
  uint8_t  apply;
  uint16_t env_ms;
  uint8_t  tap_thresh;
  uint8_t  tx_hz;
} config_t;

typedef struct __attribute__((packed)) {       // node -> base (23 bytes)
  uint8_t  device_id;
  uint32_t t_ms;
  uint16_t emg_raw;
  uint16_t emg_env;
  uint8_t  leads_off;
  int16_t  ax, ay, az;
  uint8_t  double_tap;
  uint8_t  click_src;
  uint8_t  sensor_ok;
  uint16_t active_env_ms;
  uint8_t  active_tap_thresh;
  uint8_t  active_tx_hz;
} telemetry_t;
// =====================================================================

constexpr int MAX_PLAYERS = 2;            // device_id 0 and 1
struct Player {
  bool     seen = false;
  bool     warnedDup = false;
  uint8_t  mac[6] = {0};
  telemetry_t last{};
  uint32_t lastSeenMs = 0;
  bool     tapPending = false;
};
static Player players[MAX_PLAYERS];

// ---------------- RX ----------------
static void handlePacket(const uint8_t* mac, const uint8_t* data, int len){
  if (len != (int)sizeof(telemetry_t)) return;
  telemetry_t pkt;
  memcpy(&pkt, data, sizeof(pkt));

  const int s = pkt.device_id;
  if (s < 0 || s >= MAX_PLAYERS) return;
  Player &p = players[s];

  if (!p.seen){
    p.seen = true; memcpy(p.mac, mac, 6);
    Serial.printf("# Player %d = %02X:%02X:%02X:%02X:%02X:%02X\n",
      s+1, mac[0],mac[1],mac[2],mac[3],mac[4],mac[5]);
  } else if (memcmp(p.mac, mac, 6) != 0 && !p.warnedDup){
    p.warnedDup = true;
    Serial.printf("# WARNING: DEVICE_ID %d used by more than one board\n", s);
  }

  p.last = pkt;
  p.lastSeenMs = millis();
  if (pkt.double_tap){
    p.tapPending = true;
    Serial.printf("[P%d] DOUBLE TAP (click_src=0x%02X)\n", s+1, pkt.click_src);
  }
}

#if defined(ESP_ARDUINO_VERSION) && ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3,0,0)
void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len){
  handlePacket(info->src_addr, data, len);
}
#else
void onRecv(const uint8_t *mac, const uint8_t *data, int len){
  handlePacket(mac, data, len);
}
#endif

// ---------------- config TX ----------------
static const char* tName(uint8_t t){ return t==TARGET_ALL?"all":(t==0?"P1":"P2"); }

void sendConfig(uint8_t apply, uint8_t target, uint16_t envMs, uint8_t tap, uint8_t txHz){
  config_t c{};
  c.magic = CONFIG_MAGIC; c.target_id = target; c.apply = apply;
  c.env_ms = envMs; c.tap_thresh = tap; c.tx_hz = txHz;
  esp_now_send(BCAST, (const uint8_t*)&c, sizeof(c));
}

void parseCmd(char* line){
  char cmd[16] = {0};
  long val = -1, id = -1;
  int n = sscanf(line, "%15s %ld %ld", cmd, &val, &id);
  if (n < 1) return;
  uint8_t target = (id == 0 || id == 1) ? (uint8_t)id : TARGET_ALL;

  if (strcmp(cmd, "default") == 0){
    // optional id sits in the first numeric slot here: "default" or "default 1"
    uint8_t tgt = (val == 0 || val == 1) ? (uint8_t)val : TARGET_ALL;
    sendConfig(APPLY_ENV | APPLY_TAP | APPLY_TX, tgt, 50, 0x40, 100);
    Serial.printf("# -> defaults (env=50 tap=0x40 tx=100) to %s\n", tName(tgt));
  } else if (n >= 2 && strcmp(cmd, "env") == 0){
    sendConfig(APPLY_ENV, target, (uint16_t)val, 0, 0);
    Serial.printf("# -> env=%ldms to %s\n", val, tName(target));
  } else if (n >= 2 && strcmp(cmd, "tap") == 0){
    sendConfig(APPLY_TAP, target, 0, (uint8_t)val, 0);
    Serial.printf("# -> tap=%ld to %s\n", val, tName(target));
  } else if (n >= 2 && strcmp(cmd, "tx") == 0){
    sendConfig(APPLY_TX, target, 0, 0, (uint8_t)val);
    Serial.printf("# -> tx=%ldHz to %s\n", val, tName(target));
  } else {
    Serial.println("# commands: env <ms> [id] | tap <1-127> [id] | tx <hz> [id] | default [id]  (id 0=P1 1=P2, omit=both)");
  }
}

void pollSerial(){
  static char buf[64]; static uint8_t len = 0;
  while (Serial.available()){
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n'){ buf[len] = 0; if (len) parseCmd(buf); len = 0; }
    else if (len < sizeof(buf) - 1) buf[len++] = c;
  }
}

// ---------------- setup / loop ----------------
static uint32_t nextPrintMs = 0;

void setup(){
  Serial.begin(115200);
  delay(200);

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);

  if (esp_now_init() != ESP_OK){ Serial.println("# esp_now_init FAILED"); return; }
  esp_now_register_recv_cb(onRecv);

  esp_now_peer_info_t peer{};             // needed to TX config (broadcast)
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = ESPNOW_CHANNEL;
  peer.encrypt = false;
  esp_now_add_peer(&peer);

  Serial.printf("# Flex-Off base up. MAC %s  channel %d\n",
                WiFi.macAddress().c_str(), ESPNOW_CHANNEL);
  Serial.println("# type 'env <ms>' etc to tune nodes. waiting for players...");
}

void loop(){
  pollSerial();

  const uint32_t now = millis();
  if ((int32_t)(now - nextPrintMs) < 0) return;
  nextPrintMs = now + 20;                 // 50 Hz status line

  for (int i=0;i<MAX_PLAYERS;i++){
    Player &p = players[i];
    if (!p.seen){ Serial.printf("P%d: --                                      ", i+1); continue; }
    const bool stale = (now - p.lastSeenMs) > 1000;
    Serial.printf("P%d: env=%4u leads=%d tap=%d acc=(%d,%d,%d) win=%ums%s   ",
                  i+1, (unsigned)p.last.emg_env, p.last.leads_off, p.tapPending ? 1 : 0,
                  p.last.ax, p.last.ay, p.last.az,
                  (unsigned)p.last.active_env_ms, stale ? " STALE" : "");
    p.tapPending = false;
  }
  Serial.println();
}