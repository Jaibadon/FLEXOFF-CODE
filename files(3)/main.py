import serial
import threading
import glob
import time
from collections import deque
import pyray as pr
import sys
import os
from AnimatedGIF import AnimatedGIF
from UI import draw_ui
from juice import Juice
import highscores

# Preferred port, but we auto-detect if it is missing - the mainboard can come
# up as ttyACM0 or ttyACM1 depending on what else is plugged into the Pi.
SERIAL_PORT = "/dev/ttyACM0"
BAUD_RATE = 115200
GIF_FILENAME = "assets/waterfall temple background.gif" 
is_simulating = False  
PLAYING = 4
MAX_SCORE_DIFF = 200 # must match MAX_SCORE_DIFF in the ESP32 firmware

game_data = {
    "p1_score": 0, "p2_score": 0, "current_tick": 0, "game_state": 0,
    "p1_calib": 0, "p2_calib": 0, "p1_pos": 50, "p2_pos": 50,
    "gamemode": 0, "difficulty": 1,
    "p1_mult": 1, "p2_mult": 1,
    # The board's own hit decision. None means "not reported" (simulating, or
    # older firmware), in which case the UI works it out locally instead.
    "p1_hit": None, "p2_hit": None,
    # Armband health: 0 = OK, 1 = LEADS OFF, 2 = NO LINK.
    "p1_status": 0, "p2_status": 0,
    # Raw envelope, for the live bar during calibration.
    "p1_env": 0, "p2_env": 0,
    # Power surge: 0 none, 1 armed, 2 just fired.
    "p1_surge": 0, "p2_surge": 0,
    # Seconds left in Timed Solo Rush; -1 means "no clock in this mode".
    "time_left": -1,
    # Calibration auto-start countdown, seconds; -1 when not counting.
    "ready_cd": -1,
    # Accelerometer motion level 0-100; 100 = enough shake to corrupt the EMG.
    "p1_motion": 0, "p2_motion": 0,
    # Power-surge grace window remaining, 0-100.
    "p1_grace": 0, "p2_grace": 0,
    # Indices into the high-score name word lists.
    "name_a": 0, "name_b": 0,
    # Emergency-reset hold progress, 0-100.
    "reset_hold": 0,
    # Is the Pi actually hearing the mainboard right now?
    "link_ok": False
}

calib_timers = {
    "p1_start_time": 0.0, "p2_start_time": 0.0,
    "p1_last_state": 0, "p2_last_state": 0, "countdown_start": 0.0
}

class StreakTracker:
    """
    A mirror of the ESP32's hot-streak logic, used ONLY when simulating with no
    board attached. When the real hardware is connected the firmware is the
    authority and the multiplier just arrives over serial.
    Keep these numbers in sync with STREAK_* in the ESP32 firmware.
    """
    WINDOW = 150       # 5 seconds at the 30 Hz game tick
    HITS_NEEDED = 120  # 80% of that window
    MAX_MULT = 3

    def __init__(self):
        self.reset()

    def reset(self):
        self.history = deque(maxlen=self.WINDOW)
        self.hot_ticks = 0
        self.multiplier = 1

    def update(self, hit, enabled=True):
        if not enabled:
            self.reset()
            return 1

        self.history.append(bool(hit))

        # We can only judge accuracy once we have a full window to judge.
        if len(self.history) < self.WINDOW:
            return self.multiplier

        if sum(self.history) >= self.HITS_NEEDED:
            self.multiplier = min(self.MAX_MULT, 2 + self.hot_ticks // self.WINDOW)
            # Stop counting once we're past the top tier, so this can never run away
            self.hot_ticks = min(self.hot_ticks + 1, self.WINDOW * self.MAX_MULT)
        elif self.multiplier > 1:
            # Streak broken. Wipe the history so a fresh 5 seconds must be earned.
            self.reset()

        return self.multiplier


def find_serial_port():
    """
    Return the most likely mainboard port, or None.
    Tries the configured port first, then anything that looks like a USB serial
    device. This is what lets the Pi survive the board enumerating as ttyACM1.
    """
    if os.path.exists(SERIAL_PORT):
        return SERIAL_PORT
    candidates = sorted(glob.glob("/dev/ttyACM*")) + sorted(glob.glob("/dev/ttyUSB*"))
    return candidates[0] if candidates else None


def parse_packet(line):
    """
    Turn one '<a,b,c,...>' line into a dict, or return None if it is malformed.
    Extra trailing fields are optional so this still accepts older firmware.
    """
    if not (line.startswith("<") and line.endswith(">")):
        return None
    parts = line[1:-1].split(",")
    if len(parts) < 10:
        return None

    packet = {
        "p1_score": int(parts[0]),
        "p2_score": int(parts[1]),
        "current_tick": int(parts[2]),
        "game_state": int(parts[3]),
        "p1_calib": int(parts[4]),
        "p2_calib": int(parts[5]),
        "p1_pos": int(parts[6]),
        "p2_pos": int(parts[7]),
        "gamemode": int(parts[8]),
        "difficulty": int(parts[9]),
        # Older firmware stops here, so default the rest.
        "p1_mult": 1, "p2_mult": 1,
        "p1_hit": None, "p2_hit": None,
        "p1_status": 0, "p2_status": 0,
        "p1_env": 0, "p2_env": 0,
        "p1_surge": 0, "p2_surge": 0,
        "time_left": -1, "ready_cd": -1,
        "p1_motion": 0, "p2_motion": 0,
        "p1_grace": 0, "p2_grace": 0,
        "name_a": 0, "name_b": 0, "reset_hold": 0,
        "link_ok": True,
    }

    # Fields 11/12 are the hot-streak multipliers.
    if len(parts) >= 12:
        packet["p1_mult"] = int(parts[10])
        packet["p2_mult"] = int(parts[11])

    # Fields 13/14 are the board's own on-target decision - the same one it
    # scores with, so the screen and the scoreboard can never disagree.
    if len(parts) >= 14:
        packet["p1_hit"] = int(parts[12]) == 1
        packet["p2_hit"] = int(parts[13]) == 1

    # Fields 15/16 are armband health (0 OK, 1 leads off, 2 no link).
    if len(parts) >= 16:
        packet["p1_status"] = int(parts[14])
        packet["p2_status"] = int(parts[15])

    # Fields 17/18 are the raw filtered envelope, used for the live signal bar
    # on the calibration screen (position is not meaningful until calibrated).
    if len(parts) >= 18:
        packet["p1_env"] = int(parts[16])
        packet["p2_env"] = int(parts[17])

    # Fields 19/20 are the power-surge state:
    #   0 = nothing, 1 = armed (prompt the player), 2 = just fired (celebrate).
    # If the board has SURGE_ENABLED off these stay 0 forever and the UI simply
    # never draws anything, so no Pi-side switch is needed to match it.
    if len(parts) >= 20:
        packet["p1_surge"] = int(parts[18])
        packet["p2_surge"] = int(parts[19])

    # Field 21 is the authoritative seconds remaining in Timed Solo Rush, or -1
    # in any other mode. The display used to run its own wall clock here, which
    # drifted out of step with the board's tick counter.
    if len(parts) >= 21:
        packet["time_left"] = int(parts[20])

    # Field 22 is the calibration auto-start countdown in seconds, or -1 when
    # it is not counting. Shown on screen so the wait never looks like a hang.
    if len(parts) >= 22:
        packet["ready_cd"] = int(parts[21])

    # Fields 23/24 are the accelerometer motion level, 0-100, where 100 means
    # "enough movement to be corrupting the EMG". Used for the HOLD STILL hint.
    if len(parts) >= 24:
        packet["p1_motion"] = int(parts[22])
        packet["p2_motion"] = int(parts[23])

    # Fields 25/26 are the power-surge grace window, 0-100. While it is above
    # zero the player counts as on-target even off the line, so the UI draws a
    # draining bar rather than letting it look like broken hit detection.
    if len(parts) >= 26:
        packet["p1_grace"] = int(parts[24])
        packet["p2_grace"] = int(parts[25])

    # Fields 27/28 are the two word-list indices for the high-score name
    # spinner. The board rolls the indices; the word lists live in
    # highscores.py so there is only one place to edit them.
    if len(parts) >= 28:
        packet["name_a"] = int(parts[26])
        packet["name_b"] = int(parts[27])

    # Field 29 is the emergency-reset hold progress, 0-100. Both buttons held
    # for 3 s returns the cabinet to the title screen from any state.
    if len(parts) >= 29:
        packet["reset_hold"] = int(parts[28])

    return packet


def serial_worker():
    """
    Owns the link to the mainboard for the life of the program.

    Three things this deliberately does differently from a naive reader:
      1. It BLOCKS in readline() rather than spinning on in_waiting. Polling
         with no sleep pegs a core at 100% and steals it from the renderer,
         which on a Pi shows up as dropped frames.
      2. It RECONNECTS. If the mainboard is unplugged, resets, or browns out,
         the old code raised inside the thread and died silently, leaving the
         game frozen on its last values with no way back. Now it drops the
         handle and retries until the board reappears.
      3. It reports link state, so the UI can say so instead of pretending.
    """
    global is_simulating
    ser = None
    last_rx = 0.0

    while True:
        # ---- (re)connect ----
        if ser is None:
            port = find_serial_port()
            if port is None:
                if not is_simulating:
                    print("No mainboard found - running in SIMULATION mode.")
                is_simulating = True
                game_data["link_ok"] = False
                time.sleep(1.0)
                continue
            try:
                ser = serial.Serial(port, BAUD_RATE, timeout=0.5)
                ser.reset_input_buffer()
                is_simulating = False
                last_rx = time.time()
                print("Connected to mainboard on " + port)
            except Exception:
                ser = None
                is_simulating = True
                game_data["link_ok"] = False
                time.sleep(1.0)
                continue

        # ---- read ----
        try:
            raw = ser.readline()          # blocks up to `timeout`, no busy-wait
            if raw:
                line = raw.decode("utf-8", errors="ignore").strip()
                packet = parse_packet(line)
                if packet is not None:
                    # Publish in ONE update() so the render thread can never
                    # catch a half-applied packet (new score, old position).
                    game_data.update(packet)
                    last_rx = time.time()

            # Board still enumerated but has gone quiet (crashed / wedged).
            if time.time() - last_rx > 2.0:
                game_data["link_ok"] = False

        except (serial.SerialException, OSError, TypeError) as e:
            print("Mainboard link lost (" + type(e).__name__ + ") - retrying...")
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            is_simulating = True
            game_data["link_ok"] = False
            time.sleep(0.5)
        except Exception:
            # A single corrupt line must never take the reader down.
            pass


def main():
    # 1. Set configuration flags BEFORE initializing the window.
    #    Asking for fullscreen up front avoids opening a smaller window and
    #    then toggling, which on some compositors leaves a 720p buffer being
    #    upscaled - soft edges on pixel art for no performance gain.
    pr.set_config_flags(
        pr.ConfigFlags.FLAG_VSYNC_HINT | pr.ConfigFlags.FLAG_FULLSCREEN_MODE
    )
    # 2. Init with 0, 0 - raylib then uses the native monitor resolution.
    pr.init_window(0, 0, "FLEX-OFF")
    pr.hide_cursor()                 # no mouse pointer parked on the artwork
    pr.set_exit_key(0)               # ESC must not kill a kiosk by accident
    pr.set_target_fps(60) 
    
    # --- NEW: INITIALIZE AUDIO ---
    pr.init_audio_device()
    
    theme_music = pr.load_music_stream("assets/theme_song.mp3")
    menu_music = pr.load_music_stream("assets/menu_song.mp3")
    
    sfx_start = pr.load_sound("assets/start_btn.mp3")
    sfx_cal = pr.load_sound("assets/cal_btn.mp3")
    sfx_321 = pr.load_sound("assets/countdown.mp3") # <--- Your new announcer voice!

    # "YOU'RE ON FIRE!" announcer. Optional: the game runs fine without the file.
    sfx_on_fire = None
    for ext in (".mp3", ".wav", ".ogg"):
        on_fire_path = "assets/on_fire" + ext
        if os.path.exists(on_fire_path):
            sfx_on_fire = pr.load_sound(on_fire_path)
            break
    if sfx_on_fire is None:
        print("\nNOTE: no 'assets/on_fire.mp3' found - hot streak voice line disabled.")
        print("Drop your 'you're on fire!' clip in 'assets/' named on_fire.mp3 to enable it.\n")


    WIDTH = pr.get_screen_width()
    HEIGHT = pr.get_screen_height()
    
    # -------------------------------------------------------------
    # NEW FONT LOADING LOGIC
    # -------------------------------------------------------------
    font_path = "assets/font.ttf"
    if os.path.exists(font_path):
        # 1. Calculate the absolute largest size we will need for this screen
        max_font_size = int(HEIGHT * 0.2)
        
        # 2. Use load_font_ex to generate a high-resolution texture atlas
        # The '0, 0' tells Raylib to just load the default ASCII character set
        main_font = pr.load_font_ex(font_path, max_font_size, None, 0)        
        # 3. Apply Bilinear filtering so scaling DOWN to medium/small looks perfectly smooth
        pr.set_texture_filter(main_font.texture, pr.TEXTURE_FILTER_BILINEAR)
    else:
        print("\nWARNING: 'assets/font.ttf' not found! Using default pixel font.")
        print("Download a normal font (like Roboto or Arial), name it 'font.ttf', and put it in 'assets/'.\n")
        main_font = pr.get_font_default()
        
    # Pack the font object into our fonts tuple alongside the sizes
    fonts = (int(HEIGHT * 0.2),int(HEIGHT * 0.12), int(HEIGHT * 0.08), int(HEIGHT * 0.04), main_font)
    # -------------------------------------------------------------
    
    serial_thread = threading.Thread(target=serial_worker, daemon=True)
    serial_thread.start()

    background_gif = AnimatedGIF(GIF_FILENAME, WIDTH, HEIGHT, frame_delay=100)
    
    p_width = int(WIDTH * 0.225)
    p_height = int(HEIGHT * 0.4)
    
    try:
        img = pr.load_image("assets/pixil-layer-Background.png")
        pr.image_resize_nn(img, int(WIDTH * 0.8), p_height) # Kept Sharp
        test_image = pr.load_texture_from_image(img)
        pr.set_texture_filter(test_image, pr.TEXTURE_FILTER_POINT) # Kept Sharp
        pr.unload_image(img)
    except:
        test_image = None
    
    player_2_gif = AnimatedGIF("assets/player 2.gif", p_width, p_height, frame_delay=200)
    player_1_gif = AnimatedGIF("assets/player 1.gif", p_width, p_height, frame_delay=200)
    
    easy_bot_gif = AnimatedGIF("assets/easy_bot.gif", p_width, p_height, frame_delay=200)
    medium_bot_gif = AnimatedGIF("assets/medium_bot.gif", p_width, p_height, frame_delay=200)
    hard_bot_gif = AnimatedGIF("assets/hard_bot.gif", p_width, p_height, frame_delay=200)
    
    
    
    tug_of_war_gif = AnimatedGIF("assets/tug of war.gif", int(WIDTH * 0.8), p_height, frame_delay=200)
    flag_texture = pr.load_texture("assets/flag.png")
    score_effect = pr.load_texture("assets/scoring_effect.png")
    rope_texture = pr.load_texture("assets/rope.png")
    solo_texture = pr.load_texture("assets/solo_target.png") 
    
    
    

    ui_assets = {
        "test": test_image, "bg": background_gif, "tug of war gif": tug_of_war_gif,
        "p1_gif": player_1_gif, "p2_gif": player_2_gif, "flag": flag_texture, "score_effect": score_effect, "rope": rope_texture, "solo": solo_texture,
        "easy bot": easy_bot_gif, "medium bot": medium_bot_gif, "hard bot": hard_bot_gif
    }

    # Cosmetic effects layer. Everything it does is drawn on top of an already
    # correct frame, so it can never change the score or the game state.
    fx = Juice(HEIGHT)

    # Scratch timers used only by simulation mode.
    sim_timers = {}

    # Leaderboard, loaded once and kept in ui_assets so draw_ui can see it.
    # The Pi owns this entirely: the board only spins the name and tells us
    # when a score is worth recording.
    hiscores = highscores.load()
    prev_state_for_scores = -1

    frame_count = 0 
    
     # --- NEW: TRACKING FOR SOUND EFFECTS ---
    prev_state = game_data["game_state"]
    prev_gamemode = game_data["gamemode"]
    prev_p1_calib = game_data["p1_calib"]
    prev_p2_calib = game_data["p2_calib"]

    # --- HOT STREAK TRACKING ("YOU'RE ON FIRE!") ---
    # The multiplier itself is decided by the firmware (or by these trackers when
    # simulating); here we just watch it so we can shout when it goes up.
    prev_p1_mult = 1
    prev_p2_mult = 1
    sim_p1_streak = StreakTracker()
    sim_p2_streak = StreakTracker()

    # Start playing the theme music immediately
    current_music = theme_music
    pr.play_music_stream(current_music)
    
    # ---------------------------------------
    
    while not pr.window_should_close():
        frame_count += 1
        
        # Deliberate two-key exit. A lone ESC is too easy to hit by accident on
        # a kiosk, and set_exit_key(0) above already disabled raylib's default.
        if pr.is_key_down(pr.KeyboardKey.KEY_LEFT_CONTROL) and \
           pr.is_key_pressed(pr.KeyboardKey.KEY_Q):
            break
            
        if is_simulating:
            # 1. FORCE GAME STATES (Keys 0-7)
            if pr.is_key_pressed(pr.KeyboardKey.KEY_ZERO): game_data["game_state"] = 0
            elif pr.is_key_pressed(pr.KeyboardKey.KEY_ONE): game_data["game_state"] = 1
            elif pr.is_key_pressed(pr.KeyboardKey.KEY_TWO): game_data["game_state"] = 2
            elif pr.is_key_pressed(pr.KeyboardKey.KEY_THREE): game_data["game_state"] = 3
            elif pr.is_key_pressed(pr.KeyboardKey.KEY_FOUR): game_data["game_state"] = 4
            elif pr.is_key_pressed(pr.KeyboardKey.KEY_FIVE): game_data["game_state"] = 5
            elif pr.is_key_pressed(pr.KeyboardKey.KEY_SIX): game_data["game_state"] = 6
            elif pr.is_key_pressed(pr.KeyboardKey.KEY_SEVEN): game_data["game_state"] = 7

            # 2. SIMULATE GAMEMODE SELECTION (Left/Right Arrows)
            if game_data["game_state"] == 1:
                if pr.is_key_pressed(pr.KeyboardKey.KEY_LEFT): 
                    game_data["gamemode"] = 3 if game_data["gamemode"] == 1 else game_data["gamemode"] - 1
                if pr.is_key_pressed(pr.KeyboardKey.KEY_RIGHT): 
                    game_data["gamemode"] = 1 if game_data["gamemode"] == 3 else game_data["gamemode"] + 1

            # 3b. SIMULATE THE NEW TELEMETRY so every feature is testable
            #     without the hardware present.
            #       T / Y  - toggle P1 / P2 leads-off warning
            #       G / H  - fire P1 / P2 power surge
            #       B      - toggle "no link" on P1
            if pr.is_key_pressed(pr.KeyboardKey.KEY_T):
                game_data["p1_status"] = 0 if game_data["p1_status"] == 1 else 1
            if pr.is_key_pressed(pr.KeyboardKey.KEY_Y):
                game_data["p2_status"] = 0 if game_data["p2_status"] == 1 else 1
            if pr.is_key_pressed(pr.KeyboardKey.KEY_M):
                game_data["p1_motion"] = 0 if game_data["p1_motion"] > 50 else 100
            if pr.is_key_pressed(pr.KeyboardKey.KEY_B):
                game_data["p1_status"] = 0 if game_data["p1_status"] == 2 else 2
            if pr.is_key_pressed(pr.KeyboardKey.KEY_G):
                game_data["p1_surge"] = 2
                sim_timers["surge_p1"] = pr.get_time()
            if pr.is_key_pressed(pr.KeyboardKey.KEY_H):
                game_data["p2_surge"] = 2
                sim_timers["surge_p2"] = pr.get_time()

            # The board holds SURGE_FIRED for about 0.4 s, then drops back to
            # ARMED or NONE. Mimic that so the UI is exercised the same way.
            for who in ("p1", "p2"):
                fired_at = sim_timers.get("surge_" + who, 0)
                if game_data[who + "_surge"] == 2 and fired_at:
                    if pr.get_time() - fired_at > 0.4:
                        game_data[who + "_surge"] = 1 if game_data[who + "_mult"] > 1 else 0
                        sim_timers["surge_" + who] = 0

            # 3. SIMULATE CALIBRATION PHASES (Keys C and V)
            if game_data["game_state"] == 2:
                # Press 'C' to cycle P1 Status: WAITING (0) -> CALIBRATING (1) -> READY (2)
                if pr.is_key_pressed(pr.KeyboardKey.KEY_C):
                    game_data["p1_calib"] = (game_data["p1_calib"] + 1) % 3
                    
                # Press 'V' to cycle P2 Status: WAITING (0) -> CALIBRATING (1) -> READY (2)
                if pr.is_key_pressed(pr.KeyboardKey.KEY_V):
                    game_data["p2_calib"] = (game_data["p2_calib"] + 1) % 3

            # 4. SIMULATE ARM MOVEMENT FOR GAUGES & GAMEPLAY
            # W/S for Player 1, Up/Down for Player 2
            # Notice we allow this in both state 4 (Playing) AND state 2 (Calibration Testing)
            if game_data["game_state"] in [2, 4]:
                if pr.is_key_down(pr.KeyboardKey.KEY_W): game_data["p1_pos"] = min(100, game_data["p1_pos"] + 2)
                if pr.is_key_down(pr.KeyboardKey.KEY_S): game_data["p1_pos"] = max(0, game_data["p1_pos"] - 2)
                if pr.is_key_down(pr.KeyboardKey.KEY_UP): game_data["p2_pos"] = min(100, game_data["p2_pos"] + 2)
                if pr.is_key_down(pr.KeyboardKey.KEY_DOWN): game_data["p2_pos"] = max(0, game_data["p2_pos"] - 2)

            # 5. SIMULATE GAMEPLAY TICK AND SCORING
            if game_data["game_state"] == 4:
                # The real game loop runs at 30 Hz, so tick every 2nd frame of 60
                if frame_count % 2 == 0:
                    game_data["current_tick"] += 1

                    # Mirror the firmware: score the hit, multiplied by the streak.
                    # (draw_ui published these hit flags on the previous frame.)
                    p1_hit = game_data.get("p1_is_scoring", False)
                    # Solo mode has no opponent, so P2 never scores (matches firmware)
                    p2_hit = game_data.get("p2_is_scoring", False) and game_data["gamemode"] != 3

                    game_data["p1_mult"] = sim_p1_streak.update(p1_hit)
                    game_data["p2_mult"] = sim_p2_streak.update(p2_hit, game_data["gamemode"] == 2)

                    if p1_hit: game_data["p1_score"] += game_data["p1_mult"]
                    if p2_hit: game_data["p2_score"] += game_data["p2_mult"]


                # Press 'A' to give P1 points, 'D' to give P2 points (Tests the rope flag!)
                if pr.is_key_pressed(pr.KeyboardKey.KEY_A): game_data["p1_score"] += 5
                if pr.is_key_pressed(pr.KeyboardKey.KEY_D): game_data["p2_score"] += 5                
        
        # ==========================================
        # AUDIO LOGIC (MUSIC & SFX)
        # ==========================================
        
        # 1. Determine which song should be playing
        state = game_data["game_state"]
        if state in [1, 2]:    # SELECTION, CALIBRATION
            target_music = menu_music
        elif state == 3:       # COUNTDOWN
            target_music = None   # no music during the countdown, just the voice
        else:                  # START, PLAYING, GAME_OVER, and anything unexpected
            # Defaulting here rather than listing states means a garbled packet
            # carrying an out-of-range state can never leave target_music unbound.
            target_music = theme_music
            
        # 2. Change music if needed
        if current_music != target_music:
            if current_music is not None:
                pr.stop_music_stream(current_music)
            current_music = target_music
            if current_music is not None:
                pr.play_music_stream(current_music)
            
        # 3. Update music stream
        if current_music is not None:
            pr.update_music_stream(current_music)
        
        # 4. SOUND EFFECTS: Detect State Changes
        if state != prev_state:
            
            if state == 3:
                # If we just entered the Countdown, play the 3-2-1 Voice!
                pr.play_sound(sfx_321)
            else:
                # Otherwise, it's just a normal Start button press
                pr.play_sound(sfx_start)
                
            prev_state = state
            
        # 5. SOUND EFFECTS: Detect Cal Button Presses 
        if game_data["gamemode"] != prev_gamemode:
            pr.play_sound(sfx_cal)
            prev_gamemode = game_data["gamemode"]
            
        if game_data["p1_calib"] == 1 and prev_p1_calib != 1:
            pr.play_sound(sfx_cal)
        if game_data["p2_calib"] == 1 and prev_p2_calib != 1:
            pr.play_sound(sfx_cal)
            
        prev_p1_calib = game_data["p1_calib"]
        prev_p2_calib = game_data["p2_calib"]

        # 6. SOUND EFFECTS: Hot Streak ("YOU'RE ON FIRE!")
        # Shout whenever the multiplier steps UP - on ignition (x2) and on each
        # escalation after that. Going out is silent, the badge vanishing says it.
        if state == PLAYING:
            if game_data["p1_mult"] > prev_p1_mult and sfx_on_fire is not None:
                pr.play_sound(sfx_on_fire)
            if game_data["p2_mult"] > prev_p2_mult and sfx_on_fire is not None:
                pr.play_sound(sfx_on_fire)
        elif is_simulating:
            # Not playing - make sure no streak state carries across rounds.
            # (The real firmware resets its own trackers when a match starts.)
            sim_p1_streak.reset()
            sim_p2_streak.reset()
            game_data["p1_mult"] = 1
            game_data["p2_mult"] = 1

        prev_p1_mult = game_data["p1_mult"]
        prev_p2_mult = game_data["p2_mult"]
        # ==========================================
        
            
        # --- high score submission -------------------------------------
        # Fires exactly once, on the transition INTO the leaderboard screen
        # straight after name entry. Edge-triggered so re-rendering the board
        # can never add the same run twice.
        st_now = game_data["game_state"]
        if st_now == 7 and prev_state_for_scores == 6:
            spun = highscores.name_from_seed(game_data.get("name_a", 0),
                                             game_data.get("name_b", 0))
            hiscores, rank = highscores.submit(spun, game_data["p1_score"],
                                               "SOLO")
            ui_assets["hiscores"] = hiscores
            ui_assets["hiscore_new"] = (rank - 1) if rank else None
            print("Saved '%s' with %d (rank %s)" % (spun, game_data["p1_score"], rank))
        elif st_now != 7:
            # Clear the highlight once we have left the board.
            ui_assets["hiscore_new"] = None
        prev_state_for_scores = st_now

        fx.update(pr.get_frame_time())

        pr.begin_drawing()
        pr.clear_background((20, 20, 20, 255))

        # Screen shake is applied as a camera offset around the whole frame.
        # It is only ever non-zero on the win screen - see juice.py for why it
        # is deliberately not used while someone is trying to aim.
        sx, sy = fx.shake_offset()
        if sx or sy:
            cam = pr.Camera2D()
            cam.offset = pr.Vector2(sx, sy)
            cam.target = pr.Vector2(0, 0)
            cam.rotation = 0.0
            cam.zoom = 1.0
            pr.begin_mode_2d(cam)

        ui_assets["hiscores"] = hiscores
        draw_ui(WIDTH, HEIGHT, fonts, game_data, calib_timers, ui_assets,
                MAX_SCORE_DIFF, fx)

        fx.draw_particles()
        fx.draw_popups(main_font)

        if sx or sy:
            pr.end_mode_2d()

        # Flash sits outside the shake so a full-screen tint never reveals an
        # unpainted edge when the camera is offset.
        fx.draw_flash(WIDTH, HEIGHT)

        pr.end_drawing()

    pr.close_window()
    pr.close_audio_device() 
    sys.exit()

if __name__ == "__main__":
    main()
