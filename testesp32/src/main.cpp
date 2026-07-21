#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>
#include <string.h>

// ============================================================
//  Flex-Off node: AD8232 EMG + LIS3DH tap -> ESP-NOW
//  Runtime-tunable from the base station; settings persist in flash.
//
//  Target: ESP32-C3 (GPIO0/1/3 = ADC1, GPIO20/21 = I2C, GPIO7 = INT1)
//
//  >>> SET THIS PER BOARD BEFORE FLASHING <<<
//      One board = 0, the other board = 1.
// ============================================================
constexpr uint8_t DEVICE_ID = 1;          // <-- 0 on one node, 1 on the other
static_assert(DEVICE_ID == 0 || DEVICE_ID == 1,
              "DEVICE_ID must be 0 on one board and 1 on the other");

#define DEBUG 1
#if DEBUG
  #define DBG(...)  Serial.printf(__VA_ARGS__)
#else
  #define DBG(...)
#endif

// ---------- Pin map ----------
constexpr int PIN_EMG_OUT = 3;    // AD8232 OUT  -> ADC1_CH3
constexpr int PIN_LOD_P   = 0;    // AD8232 LOD+ (HIGH = +IN electrode off)
constexpr int PIN_LOD_N   = 1;    // AD8232 LOD- (HIGH = -IN electrode off)
constexpr int PIN_I2C_SDA = 21;   // LIS3DH SDA
constexpr int PIN_I2C_SCL = 20;   // LIS3DH SCL
constexpr int PIN_INT1    = 7;    // LIS3DH INT1 (double-tap), active-high push-pull

// ---------- ESP-NOW ----------
static uint8_t PEER_MAC[6] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};  // telemetry broadcast
constexpr uint8_t ESPNOW_CHANNEL = 11;     // MUST match the base station

// ===== shared protocol : KEEP BYTE-IDENTICAL WITH THE BASE STATION =====
constexpr uint8_t CONFIG_MAGIC = 0xC5;
constexpr uint8_t APPLY_ENV = 0x01, APPLY_TAP = 0x02, APPLY_TX = 0x04;
constexpr uint8_t TARGET_ALL = 0xFF;

typedef struct __attribute__((packed)) {       // base -> node (7 bytes)
  uint8_t  magic;
  uint8_t  target_id;      // 0 / 1, or TARGET_ALL
  uint8_t  apply;          // APPLY_* bitmask
  uint16_t env_ms;         // envelope window (group delay ~ env_ms/2)
  uint8_t  tap_thresh;     // LIS3DH click threshold, 1..127
  uint8_t  tx_hz;          // telemetry rate
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
  uint16_t active_env_ms;      // echoes live settings so the base can confirm
  uint8_t  active_tap_thresh;
  uint8_t  active_tx_hz;
} telemetry_t;
// =======================================================================

// ---------- LIS3DH ----------
constexpr uint8_t LIS_ADDR = 0x19;        // SA0/SDO high
constexpr uint8_t WHO_AM_I=0x0F, CTRL1=0x20, CTRL2=0x21, CTRL3=0x22,
                  CTRL4=0x23, CTRL5=0x24, REFERENCE=0x26, OUT_X_L=0x28,
                  CLICK_CFG=0x38, CLICK_SRC=0x39, CLICK_THS=0x3A,
                  TIME_LIMIT=0x3B, TIME_LATENCY=0x3C, TIME_WINDOW=0x3D;
constexpr uint8_t TAP_THRESH_DEFAULT = 0x40;   // ~1.0 g at +/-2 g

// ---------- EMG sampling / envelope ----------
constexpr uint32_t SAMPLE_HZ   = 4000;
constexpr uint32_t SAMPLE_US   = 1000000UL / SAMPLE_HZ;
constexpr uint16_t ENV_MS_DEF  = 50, ENV_MS_MIN = 5, ENV_MS_MAX = 100;
constexpr uint32_t ENV_WIN_MAX = (SAMPLE_HZ * (uint32_t)ENV_MS_MAX) / 1000;  // 400
constexpr uint8_t  TX_HZ_DEF   = 100, TX_HZ_MIN = 10, TX_HZ_MAX = 200;
constexpr int      BASE_SHIFT  = 11;      // ~0.5 s DC tracker

static uint16_t envBuf[ENV_WIN_MAX] = {0};
static uint32_t envIdx = 0, envSum = 0;
static uint32_t envWin = (SAMPLE_HZ * ENV_MS_DEF) / 1000;   // active length (<= MAX)
static int32_t  baseQ = 0; static bool baseInit = false;
static uint32_t nextSampleUs = 0, nextTxUs = 0;
static uint32_t txUs = 1000000UL / TX_HZ_DEF;
static bool     lisOk = false;
static volatile bool tapFlag = false;

// periodic sensor health check / auto re-init
static uint32_t nextHealthMs = 0;
constexpr uint32_t HEALTH_PERIOD_MS = 2000;

// live settings reported back in telemetry
static uint16_t g_activeEnvMs    = ENV_MS_DEF;
static uint8_t  g_activeTapThresh= TAP_THRESH_DEFAULT;
static uint8_t  g_activeTxHz     = TX_HZ_DEF;

// config staging: written in the WiFi/RX task, applied in loop()
static portMUX_TYPE  cfgMux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool cfgPending = false;
static volatile uint8_t  cfgApply=0, cfgTap=0, cfgTx=0;
static volatile uint16_t cfgEnv=0;

// persistent settings
static Preferences prefs;

// ---------------- I2C helpers ----------------
void lisWrite(uint8_t reg, uint8_t val){
  Wire.beginTransmission(LIS_ADDR);
  Wire.write(reg); Wire.write(val);
  Wire.endTransmission();
}
uint8_t lisRead(uint8_t reg){
  Wire.beginTransmission(LIS_ADDR);
  Wire.write(reg);
  Wire.endTransmission(false);
  Wire.requestFrom((uint8_t)LIS_ADDR, (uint8_t)1);
  return Wire.available() ? Wire.read() : 0;
}
void lisReadXYZ(int16_t &x, int16_t &y, int16_t &z){
  Wire.beginTransmission(LIS_ADDR);
  Wire.write(OUT_X_L | 0x80);               // auto-increment
  Wire.endTransmission(false);
  Wire.requestFrom((uint8_t)LIS_ADDR, (uint8_t)6);
  uint8_t b[6]={0};
  for(int i=0;i<6 && Wire.available();i++) b[i]=Wire.read();
  x = (int16_t)(b[0] | (b[1]<<8)) >> 6;
  y = (int16_t)(b[2] | (b[3]<<8)) >> 6;
  z = (int16_t)(b[4] | (b[5]<<8)) >> 6;
}

void IRAM_ATTR onInt1(){ tapFlag = true; }

bool lisInit(){
  if (lisRead(WHO_AM_I) != 0x33) return false;
  lisWrite(CTRL1, 0x77);        // 400 Hz, normal mode, X/Y/Z enabled
  lisWrite(CTRL2, 0x04);        // HPF on the CLICK path (removes gravity)
  lisWrite(CTRL3, 0x80);        // route CLICK interrupt -> INT1
  lisWrite(CTRL4, 0x80);        // BDU=1, +/-2 g
  lisWrite(CTRL5, 0x00);
  lisWrite(CLICK_CFG, 0x2A);    // double-click on X, Y, Z
  lisWrite(CLICK_THS, 0x80 | g_activeTapThresh);  // 0x80 = latch until CLICK_SRC read
  lisWrite(TIME_LIMIT,   0x0A); // 25 ms
  lisWrite(TIME_LATENCY, 0x14); // 50 ms
  lisWrite(TIME_WINDOW,  0xFF); // 637 ms
  (void)lisRead(REFERENCE);
  (void)lisRead(CLICK_SRC);
  return true;
}

// ---------------- persistence ----------------
static uint16_t clampEnv(uint16_t ms){
  if (ms < ENV_MS_MIN) ms = ENV_MS_MIN;
  if (ms > ENV_MS_MAX) ms = ENV_MS_MAX;
  return ms;
}
static uint8_t clampTap(uint8_t t){ if (t<1) t=1; if (t>127) t=127; return t; }
static uint8_t clampTx(uint8_t hz){
  if (hz < TX_HZ_MIN) hz = TX_HZ_MIN;
  if (hz > TX_HZ_MAX) hz = TX_HZ_MAX;
  return hz;
}

void loadConfig(){
  // getX returns the default if the key was never written (fresh chip)
  g_activeEnvMs     = clampEnv(prefs.getUShort("env", ENV_MS_DEF));
  g_activeTapThresh = clampTap(prefs.getUChar ("tap", TAP_THRESH_DEFAULT));
  g_activeTxHz      = clampTx (prefs.getUChar ("tx",  TX_HZ_DEF));
  envWin = (SAMPLE_HZ * (uint32_t)g_activeEnvMs) / 1000;
  if (envWin < 1) envWin = 1;
  txUs   = 1000000UL / g_activeTxHz;
}

// ---------------- config apply (loop context: I2C + NVS safe) ----------------
void applyIfPending(){
  if (!cfgPending) return;
  portENTER_CRITICAL(&cfgMux);
  uint8_t  apply = cfgApply, tapT = cfgTap, txHz = cfgTx;
  uint16_t envMs = cfgEnv;
  cfgPending = false;
  portEXIT_CRITICAL(&cfgMux);

  if (apply & APPLY_ENV){
    uint16_t ms = clampEnv(envMs);
    uint32_t win = (SAMPLE_HZ * (uint32_t)ms) / 1000; if (win < 1) win = 1;
    memset(envBuf, 0, sizeof(envBuf));      // clean restart of the average
    envSum = 0; envIdx = 0; envWin = win; g_activeEnvMs = ms;
    prefs.putUShort("env", ms);             // NVS skips write if unchanged
  }
  if (apply & APPLY_TAP){
    uint8_t t = clampTap(tapT);
    if (lisOk) lisWrite(CLICK_THS, 0x80 | t);
    g_activeTapThresh = t;
    prefs.putUChar("tap", t);
  }
  if (apply & APPLY_TX){
    uint8_t hz = clampTx(txHz);
    txUs = 1000000UL / hz; g_activeTxHz = hz;
    prefs.putUChar("tx", hz);
  }
  DBG("# cfg applied: env=%ums tap=0x%02X tx=%uHz (saved)\n",
      (unsigned)g_activeEnvMs, (unsigned)g_activeTapThresh, (unsigned)g_activeTxHz);
}

// ---------------- ESP-NOW ----------------
#if DEBUG
void onSent(const uint8_t*, esp_now_send_status_t s){
  if (s != ESP_NOW_SEND_SUCCESS) DBG("# ESP-NOW send fail\n");
}
#endif

static void handleRx(const uint8_t* /*mac*/, const uint8_t* data, int len){
  // Only act on config frames; the other node's 23-byte telemetry is ignored.
  if (len == (int)sizeof(config_t) && data[0] == CONFIG_MAGIC){
    config_t c; memcpy(&c, data, sizeof(c));
    if (c.target_id == DEVICE_ID || c.target_id == TARGET_ALL){
      portENTER_CRITICAL(&cfgMux);
      cfgApply = c.apply; cfgEnv = c.env_ms;
      cfgTap = c.tap_thresh; cfgTx = c.tx_hz;
      cfgPending = true;
      portEXIT_CRITICAL(&cfgMux);
    }
  }
}
#if defined(ESP_ARDUINO_VERSION) && ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3,0,0)
void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len){
  handleRx(info->src_addr, data, len);
}
#else
void onRecv(const uint8_t *mac, const uint8_t *data, int len){
  handleRx(mac, data, len);
}
#endif

void espnowInit(){
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK){ DBG("# esp_now_init failed\n"); return; }
#if DEBUG
  esp_now_register_send_cb(onSent);
#endif
  esp_now_register_recv_cb(onRecv);         // receive config from the base
  esp_now_peer_info_t peer{};
  memcpy(peer.peer_addr, PEER_MAC, 6);
  peer.channel = ESPNOW_CHANNEL;
  peer.encrypt = false;
  esp_now_add_peer(&peer);
}

// ---------------- setup / loop ----------------
void setup(){
#if DEBUG
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);   // don't block on prints when no USB host is attached
  delay(200);
#endif
  prefs.begin("flexoff", false);            // RW namespace; auto-creates on first use
  loadConfig();                             // restore env / tap / tx before init

  analogReadResolution(12);
  analogSetPinAttenuation(PIN_EMG_OUT, ADC_11db);
  pinMode(PIN_LOD_P, INPUT);
  pinMode(PIN_LOD_N, INPUT);

  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 400000);
  Wire.setTimeOut(25);        // stuck bus returns instead of freezing the loop
  lisOk = lisInit();                        // uses restored g_activeTapThresh
  DBG("# node %u  LIS3DH %s\n", DEVICE_ID, lisOk ? "ok" : "NOT FOUND");

  pinMode(PIN_INT1, INPUT);
  attachInterrupt(digitalPinToInterrupt(PIN_INT1), onInt1, RISING);

  espnowInit();
  DBG("# node MAC %s  ch%d  restored env=%ums tap=0x%02X tx=%uHz\n",
      WiFi.macAddress().c_str(), ESPNOW_CHANNEL,
      (unsigned)g_activeEnvMs, (unsigned)g_activeTapThresh, (unsigned)g_activeTxHz);

  uint32_t now = micros();
  nextSampleUs = now; nextTxUs = now;
}

static uint16_t g_env=0, g_raw=0;
static uint8_t  g_pendingTap=0, g_clickSrc=0;

void sendPacket(bool leadsOff){
  int16_t ax=0, ay=0, az=0;
  if (lisOk) lisReadXYZ(ax, ay, az);

  telemetry_t p{};
  p.device_id  = DEVICE_ID;
  p.t_ms       = millis();
  p.emg_raw    = g_raw;
  p.emg_env    = g_env;
  p.leads_off  = leadsOff ? 1 : 0;
  p.ax=ax; p.ay=ay; p.az=az;
  p.double_tap = g_pendingTap;
  p.click_src  = g_clickSrc;
  p.sensor_ok  = lisOk ? 1 : 0;
  p.active_env_ms     = g_activeEnvMs;
  p.active_tap_thresh = g_activeTapThresh;
  p.active_tx_hz      = g_activeTxHz;
  esp_now_send(PEER_MAC, (uint8_t*)&p, sizeof(p));

  g_pendingTap = 0;
}

void loop(){
  applyIfPending();                     // apply + persist base-station config (safe here)
  const uint32_t now = micros();

  // ---- latched double-tap ----
  if (tapFlag){
    tapFlag = false;
    uint8_t src = lisRead(CLICK_SRC);   // reading clears the INT line
    if (src & 0x20){                    // bit5 = DCLICK
      g_pendingTap = 1;
      g_clickSrc   = src;
      DBG("# DOUBLE TAP src=0x%02X\n", src);
      sendPacket(digitalRead(PIN_LOD_P) || digitalRead(PIN_LOD_N));
    }
  }

  // ---- periodic sensor health / auto re-init (every HEALTH_PERIOD_MS) ----
  {
    const uint32_t nowMs = millis();
    if ((int32_t)(nowMs - nextHealthMs) >= 0){
      nextHealthMs = nowMs + HEALTH_PERIOD_MS;
      // WHO_AM_I confirms it answers; CTRL1 confirms it kept its config
      if (lisRead(WHO_AM_I) != 0x33 || lisRead(CTRL1) != 0x77) lisOk = false;
      if (!lisOk && lisInit()){ lisOk = true; DBG("# LIS3DH re-init ok\n"); }
    }
  }

  // ---- 4 kHz EMG sampling + envelope ----
  if ((int32_t)(now - nextSampleUs) >= 0){
    nextSampleUs += SAMPLE_US;
    const int raw = analogRead(PIN_EMG_OUT);
    if (!baseInit){ baseQ = (int32_t)raw << 16; baseInit = true; }
    baseQ += (((int32_t)raw << 16) - baseQ) >> BASE_SHIFT;
    const int base = baseQ >> 16;
    const int dev  = raw - base;
    const uint16_t rect = (uint16_t)(dev < 0 ? -dev : dev);
    envSum -= envBuf[envIdx];
    envBuf[envIdx] = rect;
    envSum += rect;
    envIdx++; if (envIdx >= envWin) envIdx = 0;
    g_env = envSum / envWin;
    g_raw = raw;
  }

  // ---- telemetry (rate = txUs) ----
  if ((int32_t)(now - nextTxUs) >= 0){
    nextTxUs += txUs;
    sendPacket(digitalRead(PIN_LOD_P) || digitalRead(PIN_LOD_N));
  }
}