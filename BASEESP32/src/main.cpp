#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>
#include <freertos/queue.h> 
#include <math.h>
#include <WiFi.h>           
#include <esp_now.h>        
#include <esp_wifi.h>       // Required to set the specific Wi-Fi channel

// ============================================================
//  Flex-Off ESP-NOW PROTOCOL (Matches C3 Transmitter Armbands)
// ============================================================
constexpr uint8_t ESPNOW_CHANNEL = 11;
static const uint8_t BCAST[6] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};

constexpr uint8_t CONFIG_MAGIC = 0xC5;
constexpr uint8_t APPLY_ENV = 0x01, APPLY_TAP = 0x02, APPLY_TX = 0x04;
constexpr uint8_t TARGET_ALL = 0xFF;

// Base -> Node config (7 bytes) - Included for future compatibility
typedef struct __attribute__((packed)) {       
  uint8_t  magic;
  uint8_t  target_id;
  uint8_t  apply;
  uint16_t env_ms;
  uint8_t  tap_thresh;
  uint8_t  tx_hz;
} config_t;

// Node -> Base telemetry (23 bytes)
typedef struct __attribute__((packed)) {       
  uint8_t  device_id; // 0 = P1, 1 = P2
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

// ==========================================
// GAME STATES ENUMERATOR
// ==========================================
enum GameState {
    STATE_TITLE_SCREEN = 0,  
    STATE_SELECTION = 1,     
    STATE_CALIBRATION = 2,   
    STATE_COUNTDOWN = 3,     
    STATE_PLAYING = 4,       
    STATE_GAME_OVER = 5      
};

// ==========================================
// PIN DEFINITIONS (ESP32-S3)
// ==========================================
#define P1_POT_PIN 11
#define P2_POT_PIN 12

#define BTN_START 5
#define BTN_CAL1  6
#define BTN_CAL2  7

#define RXp2 16
#define TXp2 17

// ==========================================
// FREERTOS VARIABLES
// ==========================================
TaskHandle_t Task_DataReceiver;
TaskHandle_t Task_GameEngine;
SemaphoreHandle_t dataMutex;
QueueHandle_t emgQueue; 

int shared_p1_raw = 0;
int shared_p2_raw = 0;

// Double-tap "event" flags. Set by DataTask, consumed (read + cleared) by GameTask.
volatile bool shared_p1_tap = false;
volatile bool shared_p2_tap = false;

// ==========================================
// GAME CONSTANTS & VARIABLES
// ==========================================
const int MAX_SCORE_DIFF = 100; 

int p1_score = 0;
int p2_score = 0;
int gamemode = 1; 
int difficulty = 1;  
GameState game_state = STATE_TITLE_SCREEN;    

int p1_calib_state = 0; 
int p2_calib_state = 0;
int p1_min = 4095, p1_max = 0;
int p2_min = 4095, p2_max = 0;

unsigned long p1_calib_start = 0;
unsigned long p2_calib_start = 0;
unsigned long game_start_time = 0;

unsigned long current_tick = 0; 
int current_level = 0;
const int HIT_TOLERANCE = 12; 

bool last_btn_start = true;
bool last_btn_cal1 = true;
bool last_btn_cal2 = true;

// --- Noise filtering: exponential moving-average low-pass on the envelope ---
// This is what stops the bar jumping around on screen. Runs at the fixed
// game-loop rate (30 Hz) so the time constant is deterministic.
float p1_env_filt = 0.0f;
float p2_env_filt = 0.0f;
const float EMG_ALPHA = 0.20f;   // lower = smoother but more lag. Try 0.10-0.30.

// --- Calibration accumulators / limits ---
// Baseline is now an AVERAGE over the relax phase (spike-proof), and the peak
// is taken from the FILTERED signal, not the raw one.
float p1_base_sum = 0.0f, p2_base_sum = 0.0f;
long  p1_base_cnt = 0,     p2_base_cnt = 0;
const int MIN_CALIB_SPAN = 150;  // min usable range. If (max - min) is smaller
                                 // than this the mapping amplifies noise, so we
                                 // clamp it. Tune to your envelope's actual scale.

// ==========================================
// AI OPPONENT GENERATOR
// ==========================================
int get_ai_position(int target_pos, int difficulty, unsigned long tick) {
    float max_error = 40.0 / difficulty; 
    float drift_speed = 0.08 + (difficulty * 0.03); 
    int mistake = (sin(tick * drift_speed) * max_error) + (cos(tick * 0.17) * (max_error/2));
    int ai_pos = target_pos + mistake;
    return constrain(ai_pos, 0, 100);
}

// ==========================================
// THE UNIVERSAL MATH ENGINE
// ==========================================
float get_target_value(unsigned long tick, int level) {
    float t = tick * 0.06; 
    float val = sin(t); 
    
    val = val * 2.0; 
    if (val > 1.0) val = 1.0;
    if (val < -1.0) val = -1.0;

    if (level >= 1) {
        val = (val * 0.6) + (sin(tick * 0.012) * 0.5); 
    }

    if (level >= 2) {
        val += sin(tick * 0.25) * 0.15;
    }

    float normalized = (val + 1.0) / 2.0;
    
    if (normalized < 0.1) normalized = 0.1;
    if (normalized > 0.9) normalized = 0.9;
    
    return normalized;
}

// ==========================================
// ESP-NOW RECEIVE CALLBACK (Version Compatible)
// ==========================================
void handlePacket(const uint8_t* mac, const uint8_t* data, int len) {
    if (len != (int)sizeof(telemetry_t)) return; // Safety check
    
    telemetry_t incoming;
    memcpy(&incoming, data, sizeof(incoming));
    
    // Shove the incoming data into the FreeRTOS Queue instantly without blocking
    xQueueSendFromISR(emgQueue, &incoming, NULL);
}

#if defined(ESP_ARDUINO_VERSION) && ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3,0,0)
void OnDataRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len){
    handlePacket(info->src_addr, data, len);
}
#else
void OnDataRecv(const uint8_t *mac, const uint8_t *data, int len){
    handlePacket(mac, data, len);
}
#endif

// ==========================================
// TASK 1: DATA RECEIVER (Core 0)
// ==========================================
void DataTask(void *pvParameters) {
    telemetry_t incomingPacket;

    // Remember the previous double_tap bit per player so a physical tap that
    // spans several packets only latches ONE event (rising-edge detect).
    static uint8_t last_tap_p1 = 0;
    static uint8_t last_tap_p2 = 0;
    
    for (;;) {
        // Wait indefinitely until a packet arrives in the Queue
        if (xQueueReceive(emgQueue, &incomingPacket, portMAX_DELAY) == pdPASS) {
            
            // We got wireless data! Lock the Mutex and update the shared variables
            if (xSemaphoreTake(dataMutex, portMAX_DELAY)) {
                
                // Using emg_env (Envelope) for smooth gameplay. 
                // Swap "emg_env" to "emg_raw" if you want the noisy raw signal instead!
                
                if (incomingPacket.device_id == 0) { // Player 1
                    shared_p1_raw = incomingPacket.emg_env; 
                    if (incomingPacket.double_tap && !last_tap_p1) shared_p1_tap = true;
                    last_tap_p1 = incomingPacket.double_tap;
                } 
                else if (incomingPacket.device_id == 1) { // Player 2
                    shared_p2_raw = incomingPacket.emg_env;
                    if (incomingPacket.double_tap && !last_tap_p2) shared_p2_tap = true;
                    last_tap_p2 = incomingPacket.double_tap;
                }
                xSemaphoreGive(dataMutex);
            }
        }
    }
}

// ==========================================
// TASK 2: GAME ENGINE (Core 1)
// ==========================================
void GameTask(void *pvParameters) {
    for (;;) {
        unsigned long current_time = millis();
        int p1_pos = 50; 
        int p2_pos = 50;

        bool btn_start_pressed = (digitalRead(BTN_START) == LOW && last_btn_start == HIGH);
        bool btn_cal1_pressed  = (digitalRead(BTN_CAL1) == LOW && last_btn_cal1 == HIGH);
        bool btn_cal2_pressed  = (digitalRead(BTN_CAL2) == LOW && last_btn_cal2 == HIGH);
        
        last_btn_start = digitalRead(BTN_START);
        last_btn_cal1  = digitalRead(BTN_CAL1);
        last_btn_cal2  = digitalRead(BTN_CAL2);

        // Grab shared data. Read + clear the tap events so each one fires exactly once.
        int p1_raw = 0;
        int p2_raw = 0;
        bool p1_tap = false;
        bool p2_tap = false;
        if (xSemaphoreTake(dataMutex, portMAX_DELAY)) {
            p1_raw = shared_p1_raw;
            p2_raw = shared_p2_raw;
            if (shared_p1_tap) { p1_tap = true; shared_p1_tap = false; }
            if (shared_p2_tap) { p2_tap = true; shared_p2_tap = false; }
            xSemaphoreGive(dataMutex); 
        }

        // --- Low-pass filter the envelope every tick (fixed 30 Hz) ---
        // This is the single biggest fix for the "bar jitters up and down" problem.
        p1_env_filt += EMG_ALPHA * (p1_raw - p1_env_filt);
        p2_env_filt += EMG_ALPHA * (p2_raw - p2_env_filt);
        int p1_val = (int)(p1_env_filt + 0.5f);   // rounded filtered value
        int p2_val = (int)(p2_env_filt + 0.5f);
        
        switch (game_state) {
            case STATE_TITLE_SCREEN:{
                // Either button press OR a double-tap starts the game.
                if (btn_start_pressed || p1_tap || p2_tap) {
                    game_state = STATE_SELECTION;
                    p1_calib_state = 0;
                    p2_calib_state = 0;
                }
                break; 
            }
            case STATE_SELECTION:{
                if (btn_cal1_pressed) {
                    gamemode--;
                    if (gamemode < 1) gamemode = 3; 
                    Serial.printf("Scrolled Left -> Mode %d\n", gamemode);
                }
                else if (btn_cal2_pressed) {
                    gamemode++;
                    if (gamemode > 3) gamemode = 1; 
                    Serial.printf("Scrolled Right -> Mode %d\n", gamemode);
                }
                else if (btn_start_pressed || p1_tap || p2_tap) {  // tap also confirms
                    game_state = STATE_CALIBRATION;
                    p1_calib_state = 0;
                    p2_calib_state = (gamemode == 2) ? 0 : 2; 
                    Serial.printf("CONFIRMED MODE: %d. Moving to Calibration.\n", gamemode);
                }
                break;
            }
            case STATE_CALIBRATION:{
                if (gamemode != 2) {
                    p2_calib_state = 2;
                }

                bool someone_is_calibrating = (p1_calib_state == 1 || p2_calib_state == 1);
                
                // A player starts THEIR OWN calibration with their CAL button OR by
                // double-tapping their armband. Only allowed if no one else is
                // mid-calibration and that player hasn't calibrated yet.
                if (!someone_is_calibrating) {
                    bool start_p1 = (btn_cal1_pressed || p1_tap) && (p1_calib_state == 0);
                    bool start_p2 = (btn_cal2_pressed || p2_tap) && (p2_calib_state == 0) && (gamemode == 2);

                    if (start_p1) {
                        p1_calib_state = 1; 
                        p1_calib_start = current_time;
                        p1_min = 4095; p1_max = 0; 
                        p1_base_sum = 0.0f; p1_base_cnt = 0;
                        Serial.println("P1 calibration started (relax, then flex).");
                    }
                    else if (start_p2) {
                        p2_calib_state = 1; 
                        p2_calib_start = current_time;
                        p2_min = 4095; p2_max = 0; 
                        p2_base_sum = 0.0f; p2_base_cnt = 0;
                        Serial.println("P2 calibration started (relax, then flex).");
                    }
                }

                // --- Player 1 calibration capture ---
                if (p1_calib_state == 1) {
                    unsigned long elapsed = current_time - p1_calib_start;
                    if (elapsed <= 3000) {
                        // RELAX: average the filtered signal for a stable baseline
                        p1_base_sum += p1_env_filt;
                        p1_base_cnt++;
                    } else if (elapsed <= 6000) {
                        // FLEX: capture the peak of the FILTERED signal (spike-proof)
                        if (p1_val > p1_max) p1_max = p1_val;
                    } else {
                        p1_min = (p1_base_cnt > 0) ? (int)(p1_base_sum / p1_base_cnt) : 0;
                        if (p1_max - p1_min < MIN_CALIB_SPAN) p1_max = p1_min + MIN_CALIB_SPAN;
                        p1_calib_state = 2; 
                        Serial.printf("P1 calibrated: min=%d max=%d\n", p1_min, p1_max);
                    }
                }

                // --- Player 2 calibration capture ---
                if (p2_calib_state == 1) {
                    unsigned long elapsed = current_time - p2_calib_start;
                    if (elapsed <= 3000) {
                        p2_base_sum += p2_env_filt;
                        p2_base_cnt++;
                    } else if (elapsed <= 6000) {
                        if (p2_val > p2_max) p2_max = p2_val;
                    } else {
                        p2_min = (p2_base_cnt > 0) ? (int)(p2_base_sum / p2_base_cnt) : 0;
                        if (p2_max - p2_min < MIN_CALIB_SPAN) p2_max = p2_min + MIN_CALIB_SPAN;
                        p2_calib_state = 2; 
                        Serial.printf("P2 calibrated: min=%d max=%d\n", p2_min, p2_max);
                    }
                }
                
                if (!someone_is_calibrating) {
                    bool ready_to_start = false;
                    
                    if (gamemode == 2) {
                        if (p1_calib_state == 2 && p2_calib_state == 2) ready_to_start = true;
                    } else {
                        if (p1_calib_state == 2) ready_to_start = true;
                    }

                    // Once everyone's calibrated, a button press OR a double-tap begins play.
                    if (ready_to_start && (btn_start_pressed || p1_tap || p2_tap)) {
                        game_state = STATE_COUNTDOWN; 
                        game_start_time = current_time;
                        p1_score = 0;
                        p2_score = 0;
                        current_tick = 0; 
                    }
                }
                break; 
            }
            case STATE_COUNTDOWN:{
                if (current_time - game_start_time >= 3000) {
                    game_state = STATE_PLAYING; 
                }
                break;
            }
            case STATE_PLAYING:{
                current_tick++;
                current_level = current_tick / 600; 

                float target_float = get_target_value(current_tick, current_level);
                int target_pos = (int)(target_float * 100.0);

                // Map the FILTERED value, not the raw one.
                p1_pos = map(p1_val, p1_min, p1_max, 0, 100);
                p1_pos = constrain(p1_pos, 0, 100);
                
                if (gamemode == 1) {
                    p2_pos = get_ai_position(target_pos, difficulty, current_tick);
                } else {
                    p2_pos = map(p2_val, p2_min, p2_max, 0, 100);
                    p2_pos = constrain(p2_pos, 0, 100);
                }

                if (abs(p1_pos - target_pos) <= HIT_TOLERANCE) p1_score++;
                if (abs(p2_pos - target_pos) <= HIT_TOLERANCE) p2_score++;
                
                if (gamemode == 1 || gamemode == 2) {
                    int score_diff = p1_score - p2_score;
                    if (abs(score_diff) >= MAX_SCORE_DIFF) {
                        game_state = STATE_GAME_OVER;    
                    }
                }
                else if (gamemode == 3) {
                    if (current_tick >= 1800) { 
                        game_state = STATE_GAME_OVER;    
                    }
                }
                break; 
            }
            case STATE_GAME_OVER:{
                // Button OR double-tap continues / restarts.
                if (btn_start_pressed || p1_tap || p2_tap) {
                    if (gamemode == 1 && p1_score > p2_score) {
                        if (difficulty < 3) {
                            difficulty += 1; 
                            game_state = STATE_COUNTDOWN; 
                            game_start_time = current_time;
                            current_tick = 0;
                        } else {
                            game_state = STATE_TITLE_SCREEN;
                            p1_calib_state = 0;
                            p2_calib_state = 0;
                            difficulty = 1; 
                        }
                    } 
                    else {
                        game_state = STATE_TITLE_SCREEN;
                        p1_calib_state = 0;
                        p2_calib_state = 0;
                        difficulty = 1; 
                        gamemode = 1; 
                    }
                    
                    p1_score = 0;
                    p2_score = 0;
                }
                break; 
            }
        } 
        
        char serialBuffer[64];
        snprintf(serialBuffer, sizeof(serialBuffer), "<%d,%d,%lu,%d,%d,%d,%d,%d,%d,%d>\n", 
                 p1_score, p2_score, current_tick, int(game_state), 
                 p1_calib_state, p2_calib_state, p1_pos, p2_pos, gamemode, difficulty);  

        Serial.print(serialBuffer); 

        vTaskDelay(pdMS_TO_TICKS(33)); 
    }
}

// ==========================================
// SETUP
// ==========================================
void setup() {
    Serial.begin(115200);                                 
    Serial2.begin(115200, SERIAL_8N1, RXp2, TXp2);        

    pinMode(BTN_START, INPUT_PULLUP);
    pinMode(BTN_CAL1, INPUT_PULLUP);
    pinMode(BTN_CAL2, INPUT_PULLUP);

    // --- ESP-NOW SETUP ---
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    
    // Crucial: Set Wi-Fi channel to match the transmitter node (Channel 11)
    esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);

    if (esp_now_init() != ESP_OK) {
        Serial.println("Error initializing ESP-NOW");
        return;
    }
    esp_now_register_recv_cb(OnDataRecv);

    // Add broadcast peer (In case you want to send configs back to the nodes later)
    esp_now_peer_info_t peer{};             
    memcpy(peer.peer_addr, BCAST, 6);
    peer.channel = ESPNOW_CHANNEL;
    peer.encrypt = false;
    esp_now_add_peer(&peer);

    // Create sync primitives BEFORE the tasks that use them.
    dataMutex = xSemaphoreCreateMutex();
    emgQueue = xQueueCreate(20, sizeof(telemetry_t));

    delay(1000);
    Serial.println("=====================================");
    Serial.print("MAINBOARD MAC ADDRESS: ");
    Serial.println(WiFi.macAddress());
    Serial.printf("LISTENING ON CHANNEL: %d\n", ESPNOW_CHANNEL);
    Serial.println("=====================================");

    xTaskCreatePinnedToCore(DataTask, "DataTask", 4096, NULL, 1, &Task_DataReceiver, 0);
    xTaskCreatePinnedToCore(GameTask, "GameTask", 8192, NULL, 1, &Task_GameEngine, 1);

    Serial.println("ESP32 Dual-Core RTOS Wireless Initialized & Ready!");
}

void loop() {
    vTaskDelete(NULL); 
}
