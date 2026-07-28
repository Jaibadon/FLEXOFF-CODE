#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>
#include <freertos/queue.h>
#include <math.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h> // Required to set the specific Wi-Fi channel

// ============================================================
// Flex-Off ESP-NOW PROTOCOL (Matches C3 Transmitter Armbands)
// ============================================================
constexpr uint8_t ESPNOW_CHANNEL = 11;
static const uint8_t BCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

constexpr uint8_t CONFIG_MAGIC = 0xC5;
constexpr uint8_t APPLY_ENV = 0x01, APPLY_TAP = 0x02, APPLY_TX = 0x04;
constexpr uint8_t TARGET_ALL = 0xFF;

// Base -> Node config (7 bytes) - Included for future compatibility
typedef struct __attribute__((packed)) {
    uint8_t magic;
    uint8_t target_id;
    uint8_t apply;
    uint16_t env_ms;
    uint8_t tap_thresh;
    uint8_t tx_hz;
} config_t;

// Node -> Base telemetry (23 bytes)
typedef struct __attribute__((packed)) {
    uint8_t device_id; // 0 = P1, 1 = P2
    uint32_t t_ms;
    uint16_t emg_raw;
    uint16_t emg_env;
    uint8_t leads_off;
    int16_t ax, ay, az;
    uint8_t double_tap;
    uint8_t click_src;
    uint8_t sensor_ok;
    uint16_t active_env_ms;
    uint8_t active_tap_thresh;
    uint8_t active_tx_hz;
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
    STATE_GAME_OVER = 5,
    // Reached only from a finished SOLO TIMED RUSH, and only when the score is
    // worth recording. Two buttons: RESPIN a random name, or CONFIRM it.
    STATE_NAME_ENTRY = 6,
    // A read-only leaderboard, reachable from the mode carousel.
    STATE_HIGHSCORES = 7
};

// ==========================================
// PIN DEFINITIONS (ESP32-S3)
// ==========================================
#define P1_POT_PIN 11
#define P2_POT_PIN 12

// ==========================================
// CONTROLS: TWO BUTTONS ONLY
// ==========================================
// The cabinet has exactly two unlabelled red buttons, one on the left of the
// panel and one on the right. Their meaning changes with the state, and the
// display always shows what each currently does, so unlabelled hardware stays
// self-explanatory:
//
//   TITLE        LEFT = START            RIGHT = START
//   MODE SELECT  LEFT = CHANGE MODE      RIGHT = CONFIRM
//   CALIBRATION  LEFT = CALIBRATE LEFT   RIGHT = CALIBRATE RIGHT
//                (starts by itself once everyone required is ready)
//   PLAYING      buttons idle - double-tap your armband for POWER SURGE
//   GAME OVER    LEFT = PLAY AGAIN       RIGHT = PLAY AGAIN
//
// Spatially the left button always belongs to the left-hand player, which is
// the one mapping nobody has to be told.
#define BTN_LEFT  6
#define BTN_RIGHT 7

// Optional third button. Nothing depends on it; wire one if you like.
#define BTN_START 5

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

// --- Electrode / link health -------------------------------------------------
// leads_off comes straight from the AD8232's LOD comparators. last_seen lets us
// notice an armband that has gone silent (flat battery, out of range, crashed)
// instead of freezing on its final reading forever.
volatile bool     shared_p1_leads_off = false;
volatile bool     shared_p2_leads_off = false;
volatile uint32_t shared_p1_last_seen = 0;
volatile uint32_t shared_p2_last_seen = 0;

// A node transmits at 100 Hz, so a third of a second of silence is already
// ~30 missed packets. That is a dead link, not a dropped frame.
const uint32_t LINK_TIMEOUT_MS = 350;

// ==========================================
// HIGH SCORE NAME SPINNER
// ==========================================
// The cabinet has two buttons and no joystick, so classic three-letter entry
// is out. Instead the board rolls two indices into word lists that live on the
// Pi (adjective + beast, e.g. "CRIMSON WYVERN"). LEFT respins, RIGHT confirms.
// Sending indices rather than text keeps the board simple and the word lists
// in exactly one place.
int name_seed_a = 0, name_seed_b = 0;

// Only a Solo Timed Rush above this is worth putting on the board. Stops the
// leaderboard filling with 3-point runs from someone who wandered off.
const int HISCORE_MIN = 20;

void roll_name() {
    name_seed_a = random(0, 24);
    name_seed_b = random(0, 24);
}

// One status per armband, reported to the Pi.
enum PlayerStatus { PSTAT_OK = 0, PSTAT_LEADS_OFF = 1, PSTAT_NO_LINK = 2 };

// ==========================================
// MOTION ARTIFACT DETECTION
// ==========================================
// The accelerometer XYZ was already being transmitted and thrown away. It is
// now used for the thing it is genuinely good for here: catching movement.
//
// Motion artifact is THE classic contaminant of surface EMG. Shaking, swinging
// or knocking the arm drags the electrode against the skin and injects a large
// low-frequency transient straight into the measurement band. The 17 Hz
// high-pass in the analog front end exists to suppress exactly this, but no
// filter removes it completely, and a hard enough shake still reads as effort.
//
// The accelerometer measures that disturbance directly, which means we can
// tell the player when their signal is being corrupted - and incidentally
// catch the obvious way to cheat, which is to shake the arm instead of
// contracting the muscle.
//
// Method: track a slow baseline of the acceleration magnitude (which is
// dominated by gravity and by however the armband happens to be oriented),
// then measure how far the instantaneous magnitude departs from it. Comparing
// against a learned baseline rather than a fixed 1g means it works regardless
// of how the band is worn.
float p1_acc_base = 0.0f, p2_acc_base = 0.0f;
float p1_motion = 0.0f, p2_motion = 0.0f;
bool  p1_acc_init = false, p2_acc_init = false;

// LSB at +/-2g, 10-bit: ~4 mg per count, so 1 g is about 256 counts.
// 45 counts is roughly 0.18 g of shake - comfortably above the wobble of a
// hard muscle contraction, comfortably below a deliberate shake.
// Raised from 45. At 45 the hint fired during ordinary vigorous play - the
// arm genuinely does move when you contract hard, and a warning you see on a
// good honest flex is just noise. 90 counts is roughly 0.35 g of shake, which
// takes a deliberate wobble rather than an energetic contraction.
const float MOTION_THRESHOLD = 90.0f;

void update_motion(int ax, int ay, int az, float &base, float &motion, bool &init) {
    float mag = sqrtf((float)ax * ax + (float)ay * ay + (float)az * az);
    if (!init) { base = mag; init = true; }
    // Slow baseline (tracks posture and orientation), fast deviation (motion).
    base += (mag - base) * 0.02f;
    float dev = fabsf(mag - base);
    motion += (dev - motion) * 0.25f;      // smooth so one sample cannot trip it
}

// Double-tap "event" flags. Set by DataTask, consumed (read + cleared) by GameTask.
volatile bool shared_p1_tap = false;
volatile bool shared_p2_tap = false;

// ==========================================
// GAME CONSTANTS & VARIABLES
// ==========================================
const int MAX_SCORE_DIFF = 200; // must match MAX_SCORE_DIFF in the Pi's main.py

int p1_score = 0;
int p2_score = 0;
int gamemode = 1;
int difficulty = 1;

// Which physical armband the human is using in a 1-player mode (0 = not claimed
// yet, 1 = armband 1, 2 = armband 2). The first one to calibrate claims it and
// the other becomes the bot. Always 1 in PvP, where both armbands are humans.
int solo_player = 0;
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

// get_target_value() only has behaviour for levels 0, 1 and 2 - past that the
// wave is identical. Cap it here so the "Speed Level" on screen tells the truth
// instead of counting up forever with nothing changing. If you add another
// difficulty tier to get_target_value(), raise this to match (in both projects).
const int MAX_LEVEL = 2;

// ==========================================
// HOT STREAK / SCORE MULTIPLIER
// ==========================================
// A player who stays on target for at least 80% of the last 5 seconds catches
// fire and starts earning multiplied points. The multiplier holds for as long
// as they keep the accuracy up, climbing one step per extra 5 seconds held,
// and collapses straight back to x1 the moment they drop below the threshold.
const int STREAK_WINDOW_TICKS = 150; // 5 seconds at the 30 Hz game loop
const int STREAK_HITS_NEEDED  = 120; // 80% of that window
const int STREAK_MAX_MULT     = 3;   // x2 on ignition, then x3

struct StreakTracker {
    bool hits[STREAK_WINDOW_TICKS] = {false}; // rolling hit/miss history
    int  idx = 0;        // where the next sample goes (circular)
    int  filled = 0;     // how much of the window is real history yet
    int  hit_count = 0;  // running total, so we never rescan the array
    int  hot_ticks = 0;  // consecutive ticks spent above the threshold
    int  multiplier = 1;
};

StreakTracker p1_streak;
StreakTracker p2_streak;

// ==========================================
// POWER SURGE  (double-tap payoff)
// ==========================================
// Catching fire ARMS a one-shot bonus. Double-tapping your own arm spends it
// for SURGE_BONUS x your current multiplier, and it can only be earned again
// by breaking the streak and rebuilding it.
//
// The interesting decision this creates: fire it at x2 for +30 now, or hold
// out for x3 and +45 and risk losing the whole streak before you get there.
//
// Balance: a match is won on a 200 point lead, and perfect play scores 30/sec
// at x1. A x3 surge is +45, about a fifth of the gap - enough to feel decisive
// in the moment, never enough to actually decide the match on its own.
// >>> ONE SWITCH TO TURN THE WHOLE FEATURE OFF <<<
// Set to false and the game behaves exactly as it did before: no bonus, no
// prompt, no celebration, and double-tap goes back to doing nothing during a
// round. Nothing else needs changing on either the board or the Pi, because
// the surge field simply reports 0 forever and the display has nothing to draw.
const bool SURGE_ENABLED = true;

// Timed Solo Rush length, in game ticks. The loop runs at 30 Hz, so 1800
// ticks is 60 seconds. This is the ONLY place the limit is defined: the
// remaining seconds are computed here and transmitted, so the display never
// runs a second clock of its own that can drift out of step with the board.
const int SOLO_TICK_LIMIT = 1800;
const int GAME_TICK_HZ = 30;

const int SURGE_BONUS = 15;
const int SURGE_FLASH_TICKS = 12;   // ~0.4s of "just fired" reported to the Pi

// After firing a surge you keep scoring for this long EVEN IF your cursor
// drifts off the line. Reaching over to whack your own arm costs you the
// tracking you spent the last few seconds earning, so without a grace window
// the reward for a perfect streak was to immediately lose it. 2 seconds is
// enough to double-tap, recover, and get back on the line.
const int SURGE_GRACE_TICKS = 60;   // 2 s at 30 Hz
int p1_surge_grace = 0, p2_surge_grace = 0;

bool p1_surge_armed = false, p2_surge_armed = false;
bool p1_surge_spent = false, p2_surge_spent = false;  // used up this streak
int  p1_surge_flash = 0,     p2_surge_flash = 0;

// What the Pi is told: 0 = nothing, 1 = armed (show the prompt), 2 = just fired.
enum SurgeState { SURGE_NONE = 0, SURGE_ARMED = 1, SURGE_FIRED = 2 };

void reset_surge() {
    p1_surge_armed = p2_surge_armed = false;
    p1_surge_spent = p2_surge_spent = false;
    p1_surge_flash = p2_surge_flash = 0;
    p1_surge_grace = p2_surge_grace = 0;
}

int p1_mult = 1;
int p2_mult = 1;

void reset_streak(StreakTracker &s) {
    for (int i = 0; i < STREAK_WINDOW_TICKS; i++) s.hits[i] = false;
    s.idx = 0;
    s.filled = 0;
    s.hit_count = 0;
    s.hot_ticks = 0;
    s.multiplier = 1;
}

// Feed one tick of hit/miss in, get the multiplier to score that tick with out.
int update_streak(StreakTracker &s, bool hit, bool enabled) {
    if (!enabled) {           // e.g. the CPU bot, which never earns a multiplier
        if (s.filled != 0) reset_streak(s);  // only wipe once, not every tick
        return 1;
    }

    // Slide the window along: drop the oldest sample, add the newest.
    if (s.filled == STREAK_WINDOW_TICKS) {
        if (s.hits[s.idx]) s.hit_count--;
    } else {
        s.filled++;
    }
    s.hits[s.idx] = hit;
    if (hit) s.hit_count++;
    s.idx = (s.idx + 1) % STREAK_WINDOW_TICKS;

    // We can only judge accuracy once we have a full window to judge.
    if (s.filled < STREAK_WINDOW_TICKS) return s.multiplier;

    if (s.hit_count >= STREAK_HITS_NEEDED) {
        s.multiplier = 2 + (s.hot_ticks / STREAK_WINDOW_TICKS);
        if (s.multiplier > STREAK_MAX_MULT) s.multiplier = STREAK_MAX_MULT;
        // Stop counting once we're past the top tier, so this can never run away
        if (s.hot_ticks < STREAK_WINDOW_TICKS * STREAK_MAX_MULT) s.hot_ticks++;
    } else if (s.multiplier > 1) {
        // Streak broken. Wipe the history so a fresh 5 seconds must be earned.
        reset_streak(s);
    }

    return s.multiplier;
}

bool last_btn_start = true;
bool last_btn_left = true;
bool last_btn_right = true;

// Mechanical buttons bounce for a few milliseconds on contact. The game loop
// samples at 30 Hz so most bounce is missed anyway, but a press landing right
// on a sample boundary could register twice and, say, skip two game modes in
// one press. A short lockout after each accepted edge removes that entirely.
const unsigned long BTN_DEBOUNCE_MS = 40;
unsigned long last_btn_left_ms = 0, last_btn_right_ms = 0, last_btn_start_ms = 0;

// Set true to print the raw state of all three button pins once a second.
// Invaluable when a button "does nothing": it tells you instantly whether the
// problem is the wiring or the game logic.
const bool BUTTON_DEBUG = false;

// ==========================================
// EMERGENCY RESET: hold BOTH buttons for 3 s
// ==========================================
// Works from any state, including mid-game. This is the "a judge has wandered
// off / something is stuck / we need the title screen NOW" escape hatch, and
// on a sealed cabinet with no keyboard it is the only one there is.
//
// Deliberately a two-button hold rather than a single long press: no single
// button can trigger it by being leaned on, and nobody discovers it by
// accident during normal play.
const unsigned long RESET_HOLD_MS = 3000;
unsigned long both_held_since = 0;
int reset_progress = 0;            // 0-100, sent to the display for a ring

// How long everyone must stay ready before calibration hands over to the
// countdown on its own. Removing a "now press START" step removes the single
// most common place a first-time player gets stuck.
//
// 10 seconds is deliberately generous: it is long enough to reposition an
// electrode, read the screen, or let a second player finish, and the remaining
// seconds are shown on screen so the wait never feels like a hang. Pressing
// your own button (or double-tapping your armband) during the wait recalibrates
// you and resets the clock.
const unsigned long AUTOSTART_MS = 10000;
unsigned long ready_since = 0;

// --- Noise filtering: exponential moving-average low-pass on the envelope ---
// This is what stops the bar jumping around on screen. Runs at the fixed
// game-loop rate (30 Hz) so the time constant is deterministic.
float p1_env_filt = 0.0f;
float p2_env_filt = 0.0f;
const float EMG_ALPHA = 0.20f; // lower = smoother but more lag. Try 0.10-0.30.

// --- Calibration accumulators / limits ---
// Baseline is now an AVERAGE over the relax phase (spike-proof), and the peak
// is taken from the FILTERED signal, not the raw one.
float p1_base_sum = 0.0f, p2_base_sum = 0.0f;
long p1_base_cnt = 0, p2_base_cnt = 0;
// How much of your calibrated flex range the game actually uses.
//
// This was a hidden 0.75, which meant the top of the highway was reached at
// only 75% of the effort you gave during calibration. The result was that you
// would calibrate carefully, watch the bar look right, then find the game
// behaved differently the moment you started playing.
//
// 1.0 means the max you flex to during calibration is exactly the max the game
// expects. Lower it if players find holding the top of the range too tiring,
// but know that it reintroduces the mismatch.
const float CALIB_HEADROOM = 1.0f;

const int MIN_CALIB_SPAN = 150; // min usable range. If (max - min) is smaller
// than this the mapping amplifies noise, so we clamp it. Tune to your envelope's actual scale.

// ==========================================
// AI OPPONENT GENERATOR
// ==========================================
int get_ai_position(int target_pos, int difficulty, unsigned long tick) {
    float max_error = 40.0 / difficulty;
    float drift_speed = 0.08 + (difficulty * 0.03);
    int mistake = (sin(tick * drift_speed) * max_error) + (cos(tick * 0.17) * (max_error / 2));
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

    // Push into the queue without blocking. NOTE: this callback runs in the
    // WiFi *task*, not an ISR, so plain xQueueSend is the correct call here -
    // xQueueSendFromISR from a task context is undefined behaviour. A timeout
    // of 0 means a full queue drops the packet rather than stalling the radio.
    xQueueSend(emgQueue, &incoming, 0);
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
                    shared_p1_leads_off = (incomingPacket.leads_off != 0);
                    shared_p1_last_seen = millis();
                    update_motion(incomingPacket.ax, incomingPacket.ay, incomingPacket.az,
                                  p1_acc_base, p1_motion, p1_acc_init);
                } 
                else if (incomingPacket.device_id == 1) { // Player 2
                    shared_p2_raw = incomingPacket.emg_env;
                    if (incomingPacket.double_tap && !last_tap_p2) shared_p2_tap = true;
                    last_tap_p2 = incomingPacket.double_tap;
                    shared_p2_leads_off = (incomingPacket.leads_off != 0);
                    shared_p2_last_seen = millis();
                    update_motion(incomingPacket.ax, incomingPacket.ay, incomingPacket.az,
                                  p2_acc_base, p2_motion, p2_acc_init);
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

        // Whether each side is on the target THIS tick. Reported to the display so
        // its "you're scoring" glow is driven by the same decision that scores.
        bool p1_on_target = false;
        bool p2_on_target = false;

        bool raw_left  = (digitalRead(BTN_LEFT)  == LOW);
        bool raw_right = (digitalRead(BTN_RIGHT) == LOW);
        bool raw_start = (digitalRead(BTN_START) == LOW);

        bool btn_left_pressed  = raw_left  && last_btn_left  == HIGH &&
                                 (current_time - last_btn_left_ms  > BTN_DEBOUNCE_MS);
        bool btn_right_pressed = raw_right && last_btn_right == HIGH &&
                                 (current_time - last_btn_right_ms > BTN_DEBOUNCE_MS);
        bool btn_start_pressed = raw_start && last_btn_start == HIGH &&
                                 (current_time - last_btn_start_ms > BTN_DEBOUNCE_MS);

        if (btn_left_pressed)  last_btn_left_ms  = current_time;
        if (btn_right_pressed) last_btn_right_ms = current_time;
        if (btn_start_pressed) last_btn_start_ms = current_time;

        last_btn_left  = raw_left  ? LOW : HIGH;
        last_btn_right = raw_right ? LOW : HIGH;
        last_btn_start = raw_start ? LOW : HIGH;

        // ---- EMERGENCY RESET ----------------------------------------------
        // Checked before anything else consumes the presses, and it SUPPRESSES
        // the individual edges while both are down. Without that, reaching for
        // the reset would also fire whatever the two buttons normally do on
        // the current screen on the way past.
        if (raw_left && raw_right) {
            if (both_held_since == 0) both_held_since = current_time;
            unsigned long held = current_time - both_held_since;
            reset_progress = (int)((held * 100) / RESET_HOLD_MS);
            if (reset_progress > 100) reset_progress = 100;

            btn_left_pressed = false;
            btn_right_pressed = false;
            btn_start_pressed = false;

            if (held >= RESET_HOLD_MS) {
                Serial.println("EMERGENCY RESET: both buttons held, returning to title.");
                game_state = STATE_TITLE_SCREEN;
                gamemode = 1;
                difficulty = 1;
                solo_player = 0;
                p1_calib_state = 0;
                p2_calib_state = 0;
                p1_score = 0;
                p2_score = 0;
                p1_mult = 1;
                p2_mult = 1;
                current_tick = 0;
                ready_since = 0;
                reset_streak(p1_streak);
                reset_streak(p2_streak);
                reset_surge();

                // Require a full release before anything counts again, so the
                // release does not immediately re-trigger on the title screen.
                both_held_since = 0;
                reset_progress = 0;
                last_btn_left = LOW;
                last_btn_right = LOW;
                last_btn_start = LOW;
            }
        } else {
            both_held_since = 0;
            reset_progress = 0;
        }

        if (BUTTON_DEBUG) {
            static unsigned long dbg_ms = 0;
            if (current_time - dbg_ms > 1000) {
                dbg_ms = current_time;
                Serial.printf("[BTN] LEFT(pin %d)=%s  RIGHT(pin %d)=%s  START(pin %d)=%s"
                              "   (DOWN means the button is being pressed)\n",
                              BTN_LEFT,  raw_left  ? "DOWN" : "up",
                              BTN_RIGHT, raw_right ? "DOWN" : "up",
                              BTN_START, raw_start ? "DOWN" : "up");
            }
        }

        // "Any button" - for the screens where both do the same thing.
        bool btn_any_pressed = btn_left_pressed || btn_right_pressed || btn_start_pressed;

        // Aliases so the calibration code reads naturally: the left button
        // calibrates the left-hand player, the right button the right-hand one.
        bool btn_cal1_pressed = btn_left_pressed;
        bool btn_cal2_pressed = btn_right_pressed;

        // Grab shared data. Read + clear the tap events so each one fires exactly once.
        int p1_raw = 0;
        int p2_raw = 0;
        bool p1_tap = false;
        bool p2_tap = false;
        bool p1_lo = false, p2_lo = false;
        uint32_t p1_seen = 0, p2_seen = 0;
        if (xSemaphoreTake(dataMutex, portMAX_DELAY)) {
            p1_raw = shared_p1_raw;
            p2_raw = shared_p2_raw;
            if (shared_p1_tap) { p1_tap = true; shared_p1_tap = false; }
            if (shared_p2_tap) { p2_tap = true; shared_p2_tap = false; }
            p1_lo   = shared_p1_leads_off;
            p2_lo   = shared_p2_leads_off;
            p1_seen = shared_p1_last_seen;
            p2_seen = shared_p2_last_seen;
            xSemaphoreGive(dataMutex); 
        }

        // Collapse electrode state and link state into one status per armband.
        // NO_LINK outranks LEADS_OFF: if we cannot hear the node at all, what
        // its electrodes were doing a second ago is not useful information.
        int p1_status = (p1_seen == 0 || (current_time - p1_seen) > LINK_TIMEOUT_MS)
                        ? PSTAT_NO_LINK : (p1_lo ? PSTAT_LEADS_OFF : PSTAT_OK);
        int p2_status = (p2_seen == 0 || (current_time - p2_seen) > LINK_TIMEOUT_MS)
                        ? PSTAT_NO_LINK : (p2_lo ? PSTAT_LEADS_OFF : PSTAT_OK);

        // --- Low-pass filter the envelope every tick (fixed 30 Hz) ---
        // This is the single biggest fix for the "bar jitters up and down" problem.
        p1_env_filt += EMG_ALPHA * (p1_raw - p1_env_filt);
        p2_env_filt += EMG_ALPHA * (p2_raw - p2_env_filt);
        int p1_val = (int)(p1_env_filt + 0.5f);   // rounded filtered value
        int p2_val = (int)(p2_env_filt + 0.5f);
        
        switch (game_state) {
            case STATE_TITLE_SCREEN: {
                // Either button, or a double-tap, starts the game.
                if (btn_any_pressed || p1_tap || p2_tap) {
                    game_state = STATE_SELECTION;
                    p1_calib_state = 0;
                    p2_calib_state = 0;
                    solo_player = 0; // nobody has claimed the player slot yet
                }
                break; 
            }
            case STATE_SELECTION: {
                // LEFT cycles the mode, RIGHT confirms. Standard two-button
                // arcade pattern, and the on-screen legend spells it out so an
                // unlabelled pair of buttons is still obvious.
                if (btn_left_pressed) {
                    gamemode++;
                    if (gamemode > 4) gamemode = 1;   // 4 = HIGH SCORES
                    Serial.printf("Changed -> Mode %d\n", gamemode);
                }
                else if (btn_right_pressed || btn_start_pressed || p1_tap || p2_tap) {
                    // Mode 4 is not a game, it is the leaderboard.
                    if (gamemode == 4) {
                        game_state = STATE_HIGHSCORES;
                        Serial.println("Showing high scores.");
                        break;
                    }
                    game_state = STATE_CALIBRATION;
                    p1_calib_state = 0;
                    p2_calib_state = 0;
                    solo_player = 0; // the slot is claimed by whoever calibrates first
                    Serial.printf("CONFIRMED MODE: %d. Moving to Calibration.\n", gamemode);
                }
                break;
            }
            case STATE_CALIBRATION: {
                bool someone_is_calibrating = (p1_calib_state == 1 || p2_calib_state == 1);

                // 0. CLAIM THE HUMAN SLOT (1-player modes only)
                // Whoever calibrates first is the real player; the other armband
                // becomes the bot and can't be calibrated from then on. The claim
                // is final once that first calibration COMPLETES - until then a
                // press of the other CAL button takes the slot over, which is the
                // way back if the wrong armband grabbed it.
                if (gamemode != 2) {
                    int claimed_state = (solo_player == 2) ? p2_calib_state : p1_calib_state;
                    bool claim_locked = (solo_player != 0 && claimed_state == 2);

                    if (!claim_locked) {
                        // Buttons can take over mid-calibration; double-taps only
                        // count when nothing is running, so a stray tap can never
                        // interrupt a calibration that's already under way.
                        int wants_slot = 0;
                        if (btn_cal1_pressed) wants_slot = 1;
                        else if (btn_cal2_pressed) wants_slot = 2;
                        else if (!someone_is_calibrating && p1_tap) wants_slot = 1;
                        else if (!someone_is_calibrating && p2_tap) wants_slot = 2;

                        if (wants_slot != 0 && wants_slot != solo_player) {
                            solo_player = wants_slot;
                            // Throw away any half-finished calibration on the slot
                            // we just took the claim away from.
                            p1_calib_state = 0;
                            p2_calib_state = 0;
                            someone_is_calibrating = false;
                            Serial.printf("Armband %d claimed the player slot; armband %d is the bot.\n",
                                          wants_slot, (wants_slot == 1) ? 2 : 1);
                        }
                    }
                }

                // Work out who is a real human this round. In PvP both are; in a
                // 1-player mode only the claimed slot is, and until someone claims
                // we assume armband 1 so nothing is locked out prematurely.
                bool p1_is_human = (gamemode == 2) || (solo_player != 2);
                bool p2_is_human = (gamemode == 2) || (solo_player == 2);

                // The bot's slot is always "ready" so it never blocks the start.
                if (!p1_is_human) p1_calib_state = 2;
                if (!p2_is_human) p2_calib_state = 2;

                // 1. TRIGGER CALIBRATION (OR RE-CALIBRATION)
                if (!someone_is_calibrating) {
                    bool start_p1 = (btn_cal1_pressed || p1_tap) && (p1_calib_state != 1) && p1_is_human;
                    bool start_p2 = (btn_cal2_pressed || p2_tap) && (p2_calib_state != 1) && p2_is_human;

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
                        // 1. Calculate the relaxed baseline
                        p1_min = (p1_base_cnt > 0) ? (int)(p1_base_sum / p1_base_cnt) : 0;
                        
                        // 2. Adjust the max so the player only has to pull 75% as hard to hit 100% on screen
                        p1_max = p1_min + (int)((p1_max - p1_min) * CALIB_HEADROOM);
                        
                        // 3. Safety check: ensure range isn't too tiny after the math
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
                        // 1. Calculate the relaxed baseline
                        p2_min = (p2_base_cnt > 0) ? (int)(p2_base_sum / p2_base_cnt) : 0;
                        
                        // 2. Adjust the max so the player only has to pull 75% as hard to hit 100% on screen
                        p2_max = p2_min + (int)((p2_max - p2_min) * CALIB_HEADROOM);

                        // 3. Safety check
                        if (p2_max - p2_min < MIN_CALIB_SPAN) p2_max = p2_min + MIN_CALIB_SPAN;
                        
                        p2_calib_state = 2; 
                        Serial.printf("P2 calibrated: min=%d max=%d\n", p2_min, p2_max);
                    }
                }

                // Player 1 Live Gauge. The human always reports on the P1 gauge,
                // whichever armband they actually claimed.
                if (p2_is_human && !p1_is_human) {
                    if (p2_calib_state == 2) {
                        p1_pos = map(p2_val, p2_min, p2_max, 0, 100);
                        p1_pos = constrain(p1_pos, 0, 100);
                    }
                } else if (p1_calib_state == 2) {
                    p1_pos = map(p1_val, p1_min, p1_max, 0, 100);
                    p1_pos = constrain(p1_pos, 0, 100);
                }

                // Player 2 Live Gauge update (Only maps if in 2-Player Mode)
                if (gamemode == 2 && p2_calib_state == 2) {
                    p2_pos = map(p2_val, p2_min, p2_max, 0, 100);
                    p2_pos = constrain(p2_pos, 0, 100);
                }

                // 2. CHECK FOR GAME START
                if (!someone_is_calibrating) {
                    bool ready_to_start = false;

                    if (gamemode == 2) {
                        if (p1_calib_state == 2 && p2_calib_state == 2) ready_to_start = true;
                    } else {
                        // Only the claimed human counts - the bot's forced "ready"
                        // must never be enough to start the game on its own.
                        int human_state = (solo_player == 2) ? p2_calib_state : p1_calib_state;
                        if (solo_player != 0 && human_state == 2) ready_to_start = true;
                    }

                    // Auto-start once everyone required has been ready for a
                    // moment. Waiting on a dedicated START press was the biggest
                    // stall point for a first-time player, and there is no third
                    // button any more. Pressing your own button during the wait
                    // recalibrates you and cancels it, which is the way back if
                    // you are unhappy with your range.
                    if (!ready_to_start) {
                        ready_since = 0;
                    } else if (ready_since == 0) {
                        ready_since = current_time;
                    }

                    bool autostart = ready_to_start && ready_since != 0 &&
                                     (current_time - ready_since >= AUTOSTART_MS);

                    if (autostart || (ready_to_start && btn_start_pressed)) {
                        ready_since = 0;
                        game_state = STATE_COUNTDOWN;
                        game_start_time = current_time;
                        p1_score = 0;
                        p2_score = 0;
                        current_tick = 0;
                        reset_streak(p1_streak);
                        reset_streak(p2_streak);
                        reset_surge();
                        p1_mult = 1;
                        p2_mult = 1;
                    }
                }
                break; 
            }
           case STATE_COUNTDOWN: {
                // 4000, not 3000: the display shows "3, 2, 1, GO!", and the GO
                // beat needs a full second of its own. At 3000 the board jumped
                // straight to PLAYING the instant the "1" finished, so "GO!"
                // was drawn for at most a single frame and usually not at all.
                if (current_time - game_start_time >= 4000) {
                    game_state = STATE_PLAYING; 
                }
                break;
            }
            case STATE_PLAYING: {
                current_tick++;
                current_level = current_tick / 600;
                if (current_level > MAX_LEVEL) current_level = MAX_LEVEL;

                float target_float = get_target_value(current_tick, current_level);
                int target_pos = (int)(target_float * 100.0);

                // The human always plays as P1 on screen, even if they claimed
                // armband 2 back in calibration. Map the FILTERED value, not raw.
                bool human_on_p2 = (gamemode != 2 && solo_player == 2);
                int h_val = human_on_p2 ? p2_val : p1_val;
                int h_min = human_on_p2 ? p2_min : p1_min;
                int h_max = human_on_p2 ? p2_max : p1_max;

                p1_pos = map(h_val, h_min, h_max, 0, 100);
                p1_pos = constrain(p1_pos, 0, 100);

                if (gamemode == 1) {
                    p2_pos = get_ai_position(target_pos, difficulty, current_tick);
                } else if (gamemode == 2) {
                    p2_pos = map(p2_val, p2_min, p2_max, 0, 100);
                    p2_pos = constrain(p2_pos, 0, 100);
                }
                // Solo mode has no opponent at all, so p2_pos stays at a neutral 50.

                p1_on_target = abs(p1_pos - target_pos) <= HIT_TOLERANCE;
                // ...and with no opponent, nothing on P2's side may ever score.
                p2_on_target = (gamemode != 3) && (abs(p2_pos - target_pos) <= HIT_TOLERANCE);

                // ---- SURGE GRACE ----------------------------------------
                // While the grace window is open you count as on-target even
                // if you have drifted off the line, so reaching over to whack
                // your own arm does not immediately cost you the streak you
                // spent several seconds building. The window ticks down every
                // frame regardless.
                bool p1_hit = p1_on_target || (p1_surge_grace > 0);
                bool p2_hit = p2_on_target || (p2_surge_grace > 0);
                if (p1_surge_grace > 0) p1_surge_grace--;
                if (p2_surge_grace > 0) p2_surge_grace--;

                // The screen shows the grace as a draining bar, so it never
                // looks like the hit detection has simply gone wrong.
                p1_on_target = p1_hit;
                p2_on_target = p2_hit;

                // Only human players earn multipliers - the bot stays on x1.
                p1_mult = update_streak(p1_streak, p1_hit, true);
                p2_mult = update_streak(p2_streak, p2_hit, gamemode == 2);

                if (p1_hit) p1_score += p1_mult;
                if (p2_hit) p2_score += p2_mult;

                // ---- POWER SURGE ------------------------------------------
                // In a 1-player mode the human may physically be wearing
                // armband 2, so route the tap the same way the position is
                // routed - otherwise their own tap would do nothing.
                bool human_tap_p1 = (gamemode != 2 && solo_player == 2) ? p2_tap : p1_tap;
                if (!SURGE_ENABLED) { human_tap_p1 = false; }

                // Catching fire arms the bonus; dropping the streak clears it
                // AND re-earns the right to use one on the next streak.
                if (p1_mult >= 2 && SURGE_ENABLED) {
                    if (!p1_surge_spent) p1_surge_armed = true;
                } else {
                    p1_surge_armed = false;
                    p1_surge_spent = false;
                }
                if (human_tap_p1 && p1_surge_armed) {
                    p1_score += SURGE_BONUS * p1_mult;
                    p1_surge_armed = false;
                    p1_surge_spent = true;      // one per streak
                    p1_surge_flash = SURGE_FLASH_TICKS;
                    p1_surge_grace = SURGE_GRACE_TICKS;
                }
                if (p1_surge_flash > 0) p1_surge_flash--;

                // P2 only exists as a real tapping human in PvP.
                if (gamemode == 2 && SURGE_ENABLED) {
                    if (p2_mult >= 2) {
                        if (!p2_surge_spent) p2_surge_armed = true;
                    } else {
                        p2_surge_armed = false;
                        p2_surge_spent = false;
                    }
                    if (p2_tap && p2_surge_armed) {
                        p2_score += SURGE_BONUS * p2_mult;
                        p2_surge_armed = false;
                        p2_surge_spent = true;
                        p2_surge_flash = SURGE_FLASH_TICKS;
                        p2_surge_grace = SURGE_GRACE_TICKS;
                    }
                    if (p2_surge_flash > 0) p2_surge_flash--;
                }
                
                if (gamemode == 1 || gamemode == 2) {
                    int score_diff = p1_score - p2_score;
                    if (abs(score_diff) >= MAX_SCORE_DIFF) {
                        game_state = STATE_GAME_OVER;    
                    }
                }
                else if (gamemode == 3) {
                    if (current_tick >= SOLO_TICK_LIMIT) {
                        // A qualifying Solo Timed Rush goes directly to the
                        // name spinner. Keep p1_score intact: the Pi reads the
                        // final score while the player chooses/confirms a name.
                        if (p1_score >= HISCORE_MIN) {
                            roll_name();
                            game_state = STATE_NAME_ENTRY;
                            Serial.printf("Timed Rush complete: score %d qualifies - name entry.\n", p1_score);
                        } else {
                            game_state = STATE_GAME_OVER;
                        }
                    }
                }
                break; 
            }
            case STATE_NAME_ENTRY: {
                // LEFT respins, RIGHT (or a double-tap) accepts. The Pi does
                // the actual saving; the board only owns the spinner.
                if (btn_left_pressed) {
                    roll_name();
                }
                else if (btn_right_pressed || btn_start_pressed || p1_tap || p2_tap) {
                    game_state = STATE_HIGHSCORES;
                    Serial.println("Name confirmed - showing the board.");
                }
                break;
            }
            case STATE_HIGHSCORES: {
                // Read-only. Any button leaves.
                if (btn_any_pressed || p1_tap || p2_tap) {
                    game_state = STATE_TITLE_SCREEN;
                    p1_calib_state = 0;
                    p2_calib_state = 0;
                    difficulty = 1;
                    gamemode = 1;
                    solo_player = 0;
                }
                break;
            }
            case STATE_GAME_OVER: {
                // LEFT  = PLAY AGAIN  -> straight back into the same mode at the
                //                       same difficulty, keeping both players'
                //                       calibration. Nobody wants to re-run a
                //                       6-second relax/flex just to have another go.
                // RIGHT = MAIN MENU   -> full reset back to the title screen.
                //
                // A double-tap counts as PLAY AGAIN, because that is what someone
                // still wearing an armband almost always wants.
                bool want_replay = btn_left_pressed || p1_tap || p2_tap;
                bool want_menu   = btn_right_pressed || btn_start_pressed;

                bool p1_beat_bot = (p1_score > p2_score);

                if (want_replay || want_menu) {
                    // Shared reset: scores, streaks and surge always clear.
                    p1_score = 0;
                    p2_score = 0;
                    reset_streak(p1_streak);
                    reset_streak(p2_streak);
                    reset_surge();
                    p1_mult = 1;
                    p2_mult = 1;
                    current_tick = 0;
                    ready_since = 0;

                    if (want_replay) {
                        // Keep gamemode, solo_player AND the calibration
                        // (p1_min/max, p2_min/max) exactly as they were, and go
                        // straight to the countdown. Nobody wants to re-run a
                        // 6-second relax/flex just to have another go.
                        //
                        // One exception, kept from the original design: beating
                        // a CPU bot promotes you to the next difficulty, so the
                        // ladder still works. The display labels the button
                        // NEXT LEVEL rather than PLAY AGAIN when that applies.
                        if (gamemode == 1 && p1_beat_bot && difficulty < 3) {
                            difficulty += 1;
                            Serial.printf("NEXT LEVEL: difficulty %d, calibration kept.\n", difficulty);
                        } else {
                            Serial.println("PLAY AGAIN: same mode, same calibration.");
                        }
                        game_state = STATE_COUNTDOWN;
                        game_start_time = current_time;
                    } else {
                        // Full reset. Calibration values are left in place but
                        // the states are cleared, so the next round recalibrates.
                        game_state = STATE_TITLE_SCREEN;
                        p1_calib_state = 0;
                        p2_calib_state = 0;
                        difficulty = 1;
                        gamemode = 1;
                        solo_player = 0;
                        Serial.println("MAIN MENU: full reset.");
                    }
                }
                break;
            }
        }
        
        // The display always draws the human on the left, so if they claimed
        // armband 2 in a 1-player mode we report their calibration as P1's and
        // show the bot's slot as permanently ready.
        int rep_p1_calib  = p1_calib_state;
        int rep_p2_calib  = p2_calib_state;
        int rep_p1_status = p1_status;
        int rep_p2_status = p2_status;
        if (gamemode != 2 && solo_player == 2) {
            rep_p1_calib = p2_calib_state;
            rep_p2_calib = 2;
            // The human is on armband 2 but is always drawn on the left, so
            // their electrode warning has to move to the left with them.
            rep_p1_status = p2_status;
        }
        if (gamemode != 2) {
            // The CPU bot and the solo target have no armband, so they can
            // never have an electrode fault. Suppress that slot's warning.
            rep_p2_status = PSTAT_OK;
        }

        // Raw filtered envelope, sent so the display can show a live signal
        // bar DURING calibration. Before calibration finishes there is no
        // min/max to map a 0-100 position from, so without this the player
        // gets no feedback at all while relaxing and flexing - which reads as
        // "is this thing even on?". Clamped to 4095 to keep the field width
        // bounded no matter what the front end does.
        int rep_p1_env = (int)constrain(p1_env_filt, 0.0f, 4095.0f);
        int rep_p2_env = (int)constrain(p2_env_filt, 0.0f, 4095.0f);
        if (gamemode != 2 && solo_player == 2) rep_p1_env = rep_p2_env;

        // Surge: 2 = just fired (celebrate), 1 = armed (prompt), 0 = nothing.
        int rep_p1_surge = p1_surge_flash > 0 ? SURGE_FIRED
                         : (p1_surge_armed ? SURGE_ARMED : SURGE_NONE);
        int rep_p2_surge = p2_surge_flash > 0 ? SURGE_FIRED
                         : (p2_surge_armed ? SURGE_ARMED : SURGE_NONE);
        if (gamemode != 2) rep_p2_surge = SURGE_NONE;   // the bot never surges
        if (!SURGE_ENABLED) { rep_p1_surge = SURGE_NONE; rep_p2_surge = SURGE_NONE; }

        // Seconds left in Timed Solo Rush, authoritative. Sent as -1 in every
        // other mode so the display knows there is no clock to show rather
        // than having to infer it from the game mode.
        int rep_time_left = -1;
        if (gamemode == 3) {
            long ticks_left = (long)SOLO_TICK_LIMIT - (long)current_tick;
            if (ticks_left < 0) ticks_left = 0;
            // Round up, so the clock reads "1" for the whole final second and
            // hits 0 exactly when the round actually ends.
            rep_time_left = (int)((ticks_left + GAME_TICK_HZ - 1) / GAME_TICK_HZ);
        }

        // Seconds left before calibration auto-starts, or -1 when not counting.
        // Sent so the display can show a real countdown instead of the player
        // staring at a "ready" screen wondering whether anything will happen.
        int rep_ready_cd = -1;
        if (game_state == STATE_CALIBRATION && ready_since != 0) {
            long ms_left = (long)AUTOSTART_MS - (long)(current_time - ready_since);
            if (ms_left < 0) ms_left = 0;
            rep_ready_cd = (int)((ms_left + 999) / 1000);   // round up
        }

        // Motion level, 0-100, where 100 means "at the threshold or beyond".
        // Reported rather than acted on: a false positive that silently stops
        // someone scoring would be far worse than a warning they can ignore.
        int rep_p1_motion = (int)constrain(p1_motion * 100.0f / MOTION_THRESHOLD, 0.0f, 100.0f);
        int rep_p2_motion = (int)constrain(p2_motion * 100.0f / MOTION_THRESHOLD, 0.0f, 100.0f);
        if (gamemode != 2 && solo_player == 2) rep_p1_motion = rep_p2_motion;
        if (gamemode != 2) rep_p2_motion = 0;

        // Remaining surge grace as a percentage, for the draining bar.
        int rep_p1_grace = (p1_surge_grace * 100) / SURGE_GRACE_TICKS;
        int rep_p2_grace = (p2_surge_grace * 100) / SURGE_GRACE_TICKS;
        if (gamemode != 2 && solo_player == 2) rep_p1_grace = rep_p2_grace;
        if (gamemode != 2) rep_p2_grace = 0;

        char serialBuffer[256];
        snprintf(serialBuffer, sizeof(serialBuffer), "<%d,%d,%lu,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d>\n",
                 p1_score, p2_score, current_tick, int(game_state),
                 rep_p1_calib, rep_p2_calib, p1_pos, p2_pos, gamemode, difficulty,
                 p1_mult, p2_mult, p1_on_target ? 1 : 0, p2_on_target ? 1 : 0,
                 rep_p1_status, rep_p2_status, rep_p1_env, rep_p2_env,
                 rep_p1_surge, rep_p2_surge, rep_time_left, rep_ready_cd,
                 rep_p1_motion, rep_p2_motion, rep_p1_grace, rep_p2_grace,
                 name_seed_a, name_seed_b, reset_progress);

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

    randomSeed(esp_random());   // so the name spinner differs every power-up
    pinMode(BTN_LEFT,  INPUT_PULLUP);
    pinMode(BTN_RIGHT, INPUT_PULLUP);
    pinMode(BTN_START, INPUT_PULLUP);   // optional third button, unused

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
