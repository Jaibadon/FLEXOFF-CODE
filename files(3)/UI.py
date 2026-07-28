import pyray as pr
import time
import math
from enum import IntEnum

import attract
import highscores


# We use IntEnum to give readable names to our game states.
# Under the hood, START_SCREEN is just 0, SELECTION is 1, etc.
class GameState(IntEnum):
    START_SCREEN = 0
    SELECTION = 1
    CALIBRATION = 2
    COUNTDOWN = 3
    PLAYING = 4
    GAME_OVER = 5
    NAME_ENTRY = 6      # spin a random name for a qualifying solo score
    HIGHSCORES = 7      # read-only leaderboard


# --- COLOR DEFINITIONS ---
# Colors are formatted as (Red, Green, Blue, Alpha/Transparency)
# Values range from 0 to 255. Alpha 255 means fully solid.
BLACK = (20, 20, 20, 255)
WHITE = (240, 240, 240, 255)
BLUE = (50, 100, 200, 255)      
BLUE_BRIGHT = (100, 200, 255, 255)
RED = (200, 50, 50, 255)        
RED_BRIGHT = (255, 100, 100, 255)  
GREEN = (50, 255, 50, 255)
YELLOW = (255, 200, 50, 255)
GRAY = (100, 100, 100, 255)
ROPE_COLOR = (200, 180, 140, 255)
FIRE_ORANGE = (255, 140, 30, 255)
FIRE_YELLOW = (255, 220, 90, 255)
FIRE_DARK = (45, 15, 0, 230)


# --- Armband health, mirrored from the firmware's PlayerStatus enum ---
STAT_OK = 0
STAT_LEADS_OFF = 1
STAT_NO_LINK = 2

ALERT_RED = (220, 40, 40, 255)
ALERT_AMBER = (255, 170, 40, 255)


def draw_surge_grace(cx, cy, radius, level, current_time):
    """
    Ring around the cursor showing the power-surge grace window draining.

    While this is up the player scores even off the line, which without a
    visual would look exactly like broken hit detection. The ring shrinks and
    shifts hue from violet through amber to red as it runs out, so the player
    can see at a glance how long they have to get back on the target.
    """
    if level <= 0:
        return
    f = level / 100.0

    # Violet (fresh) -> amber -> red (about to expire).
    if f > 0.5:
        k = (f - 0.5) * 2.0
        col = (int(255 - 65 * k), int(170 - 50 * k), int(40 + 215 * k))
    else:
        k = f * 2.0
        col = (int(235 + 20 * k), int(60 + 110 * k), int(60 - 20 * k))

    pulse = 0.5 + 0.5 * math.sin(current_time * 14.0)
    outer = radius * (1.9 + 0.9 * f)

    # Soft halo, then the ring itself thinning as the window closes.
    pr.draw_circle(int(cx), int(cy), outer, (col[0], col[1], col[2], int(40 * f)))
    pr.draw_circle_lines(int(cx), int(cy), outer, (col[0], col[1], col[2], int(160 + 80 * pulse)))
    pr.draw_circle_lines(int(cx), int(cy), outer - 2, (col[0], col[1], col[2], int(120 * f)))


def draw_motion_warning(font, cx, cy, w, h, level, current_time):
    """
    "HOLD STILL" hint, driven by the armband accelerometer.

    Motion artifact is the classic contaminant of surface EMG: shaking or
    knocking the arm drags the electrode on the skin and injects a transient
    straight into the measurement band. The analog high-pass suppresses it but
    cannot remove it, so a hard enough shake still reads as effort. The
    accelerometer measures that disturbance directly, which lets the game tell
    a player their signal is being corrupted - and incidentally catches the
    obvious cheat of shaking instead of contracting.

    Advisory only. A false positive that silently stopped someone scoring would
    be much worse than a hint they can ignore.
    """
    if level < 60:
        return
    # Fades in between 60 and 100 so it does not pop on abruptly.
    strength = min(1.0, (level - 60) / 40.0)
    pulse = 0.5 + 0.5 * math.sin(current_time * 11.0)
    a = int((110 + 90 * pulse) * strength)
    amber = (255, 170, 40)

    pr.draw_rectangle_rounded((cx - w / 2, cy - h / 2, w, h), 0.35, 8, (30, 20, 0, int(200 * strength)))
    pr.draw_rectangle_rounded_lines((cx - w / 2, cy - h / 2, w, h), 0.35, 8,
                                    (amber[0], amber[1], amber[2], a))
    draw_centered_text(font, "HOLD STILL", cx, cy - h * 0.13, h * 0.42,
                       (amber[0], amber[1], amber[2], min(255, a + 60)))
    draw_centered_text(font, "shaking is not flexing", cx, cy + h * 0.26, h * 0.22,
                       (225, 225, 225, int(200 * strength)), False)


def draw_status_alert(main_font, cx, top_y, w, h, status, current_time, label):
    """
    A compact warning strip pinned to one player's half of the screen.

    Deliberately small and high up: an electrode popping off mid-round is
    information the player needs instantly, but blanking the screen over it
    would be worse than the fault. It pulses so it catches the eye in
    peripheral vision while someone is concentrating on the highway.
    """
    if status == STAT_OK:
        return

    if status == STAT_LEADS_OFF:
        colour, title, sub = ALERT_RED, label + " LEADS OFF", "CHECK ELECTRODES"
    else:
        colour, title, sub = ALERT_AMBER, label + " NO SIGNAL", "CHECK ARMBAND POWER"

    # Two-speed pulse: the border throbs faster than the fill so it reads as
    # an alarm rather than a decoration.
    pulse = 0.5 + 0.5 * math.sin(current_time * 9.0)
    border = (colour[0], colour[1], colour[2], int(150 + 105 * pulse))
    body = (25, 5, 5, 225)

    thick = max(2.0, h * 0.07)
    pr.draw_rectangle_rounded((cx - w / 2 - thick, top_y - thick,
                               w + thick * 2, h + thick * 2), 0.25, 8, border)
    pr.draw_rectangle_rounded((cx - w / 2, top_y, w, h), 0.25, 8, body)

    # Hazard stripes down each edge, arcade cabinet style.
    stripe_w = w * 0.035
    for side in (-1, 1):
        sx = cx + side * (w / 2 - stripe_w * 1.6)
        pr.draw_rectangle(int(sx - stripe_w / 2), int(top_y + h * 0.18),
                          int(stripe_w), int(h * 0.64), border)

    draw_centered_text(main_font, title, cx, top_y + h * 0.34, h * 0.34, colour)
    draw_centered_text(main_font, sub, cx, top_y + h * 0.70, h * 0.21,
                       (235, 235, 235, 255), False)


# get_target_value() only has behaviour for levels 0, 1 and 2 - past that the
# wave is identical, so the on-screen "Speed Level" is capped here rather than
# counting up forever with nothing actually changing. Keep this in sync with
# MAX_LEVEL in the ESP32 firmware.
MAX_LEVEL = 2


def get_target_value(tick, level):
    """
    Calculates where the target line should be at a specific moment in time (tick).
    Returns a float between 0.1 (bottom) and 0.9 (top).
    """
    # Base sine wave (smooth up and down motion)
    t = tick * 0.06
    val = math.sin(t)
    val = val * 2.0
   
    # Cap the value so it doesn't go too crazy
    if val > 1.0: val = 1.0
    if val < -1.0: val = -1.0
   
    # As the level increases, we add faster, smaller sine waves on top of the base wave.
    # This creates "bumps" and erratic movements, making it harder to track.
    if level >= 1:
        val = (val * 0.6) + (math.sin(tick * 0.012) * 0.5)
    if level >= 2:
        val += math.sin(tick * 0.25) * 0.15
       
    # Sine waves go from -1.0 to 1.0. We normalize this to 0.0 to 1.0 so we can
    # easily use it as a percentage on the screen.
    normalized = (val + 1.0) / 2.0
   
    # Keep the target slightly away from the absolute top (1.0) and bottom (0.0) edges
    return max(0.1, min(0.9, normalized))


# NEW: Advanced text rendering supporting custom TTF fonts and Faux Bold
def draw_centered_text(font, text, x, y, font_size, color, is_bold=True):
    spacing = font_size / 10.0
    text_size = pr.measure_text_ex(font, text, float(font_size), spacing)
   
    pos_x = x - (text_size.x / 2)
    pos_y = y - (text_size.y / 2)
   
    # If bold is True, we draw the text a few extra times, slightly offset
    if is_bold:
        # Calculate how thick the bold should be based on font size (usually 1 to 3 pixels)
        offset = max(1.0, font_size * 0.03)
       
        # Draw slightly to the right, down, and diagonal to thicken the letters
        pr.draw_text_ex(font, text, (pos_x + offset, pos_y), float(font_size), spacing, color)
        pr.draw_text_ex(font, text, (pos_x, pos_y + offset), float(font_size), spacing, color)
        pr.draw_text_ex(font, text, (pos_x + offset, pos_y + offset), float(font_size), spacing, color)


    # Draw the standard centered text over the top
    pr.draw_text_ex(font, text, (pos_x, pos_y), float(font_size), spacing, color)
   
   
   
def draw_fire_glow(x, y, w, h, current_time):
    """A pulsing orange halo drawn behind a player's highway while they're on fire."""
    pulse = 0.5 + 0.5 * math.sin(current_time * 10.0)
    for i in (3, 2, 1):
        pad = i * (h * 0.012)
        alpha = int((40 + 70 * pulse) / i)
        pr.draw_rectangle_rounded((x - pad, y - pad, w + pad * 2, h + pad * 2),
                                  0.2, 10, (255, 140, 30, alpha))


def draw_multiplier_badge(main_font, center_x, center_y, badge_w, badge_h, mult, current_time):
    """The 'ON FIRE! xN' badge that tells the player their points are multiplied."""
    pulse = 0.5 + 0.5 * math.sin(current_time * 10.0)

    # Text is sized off the badge itself so both lines always fit inside it,
    # whatever resolution the screen happens to be.
    label_size = badge_h * 0.28
    mult_size = badge_h * 0.45

    # Border is drawn as a slightly larger rounded rect behind the body, so its
    # thickness can breathe with the pulse.
    thick = (badge_h * 0.05) + (badge_h * 0.05 * pulse)
    border_color = (255, int(140 + 80 * pulse), int(30 + 70 * pulse), 255)

    pr.draw_rectangle_rounded((center_x - badge_w / 2 - thick, center_y - badge_h / 2 - thick,
                               badge_w + thick * 2, badge_h + thick * 2), 0.3, 10, border_color)
    pr.draw_rectangle_rounded((center_x - badge_w / 2, center_y - badge_h / 2,
                               badge_w, badge_h), 0.3, 10, FIRE_DARK)

    draw_centered_text(main_font, "ON FIRE!", center_x, center_y - badge_h * 0.25, label_size, border_color)
    draw_centered_text(main_font, f"x{mult}", center_x, center_y + badge_h * 0.18, mult_size, FIRE_YELLOW)


BTN_RED = (225, 55, 55, 255)
BTN_RED_DARK = (120, 22, 22, 255)


def draw_scoreboard(font, WIDTH, HEIGHT, entries, top_y, row_h,
                    highlight=None, title="HIGH SCORES"):
    """
    The leaderboard. `highlight` is an index to pick out (a name just added),
    so a player can find themselves instantly instead of scanning the list.
    """
    draw_centered_text(font, title, WIDTH / 2, top_y - row_h * 1.1,
                       row_h * 0.95, YELLOW)

    if not entries:
        draw_centered_text(font, "NO SCORES YET", WIDTH / 2, top_y + row_h * 1.5,
                           row_h * 0.7, GRAY)
        draw_centered_text(font, "play SOLO TIMED RUSH to set one",
                           WIDTH / 2, top_y + row_h * 2.6, row_h * 0.42, GRAY, False)
        return

    panel_w = WIDTH * 0.52
    px = WIDTH / 2 - panel_w / 2

    for i, e in enumerate(entries):
        y = top_y + i * row_h
        is_me = (highlight is not None and i == highlight)

        if is_me:
            # Pulse the new entry so it is impossible to miss.
            a = int(70 + 60 * (0.5 + 0.5 * math.sin(pr.get_time() * 6.0)))
            pr.draw_rectangle_rounded((px - 8, y - row_h * 0.42, panel_w + 16, row_h * 0.86),
                                      0.4, 8, (255, 200, 60, a))
        elif i % 2 == 0:
            pr.draw_rectangle_rounded((px - 8, y - row_h * 0.42, panel_w + 16, row_h * 0.86),
                                      0.4, 8, (255, 255, 255, 12))

        rank_col = (255, 215, 90, 255) if i == 0 else (
                   (215, 215, 225, 255) if i == 1 else (
                   (205, 140, 80, 255) if i == 2 else GRAY))
        size = row_h * 0.60

        draw_centered_text(font, "%d" % (i + 1), px + panel_w * 0.06, y, size, rank_col)
        # Name left-aligned by drawing it centred on its own half-width.
        spacing = size / 10.0
        nw = pr.measure_text_ex(font, e["name"], float(size), spacing).x
        draw_centered_text(font, e["name"], px + panel_w * 0.15 + nw / 2, y, size,
                           WHITE if not is_me else (255, 245, 200, 255), False)
        sw = pr.measure_text_ex(font, str(e["score"]), float(size), spacing).x
        draw_centered_text(font, str(e["score"]), px + panel_w * 0.94 - sw / 2, y,
                           size, GREEN if not is_me else (255, 245, 200, 255))


def draw_button_legend(font, WIDTH, HEIGHT, left_label, right_label, current_time):
    """
    The panel has two unlabelled red buttons, one left and one right, and their
    meaning changes with the state. Rather than printing labels on the cabinet
    (which would then be wrong on half the screens), the display shows what each
    one does RIGHT NOW, drawn at the bottom-left and bottom-right so the
    on-screen position matches where your hands actually are.

    Pass None for a side that currently does nothing.
    """
    # Pinned to the TOP of the screen. The physical buttons sit on top of the
    # cabinet, so putting their labels along the top edge keeps the on-screen
    # position matching where your hands actually are.
    bar_h = HEIGHT * 0.085
    bar_y = 0.0
    pr.draw_rectangle(0, 0, int(WIDTH), int(bar_h), (0, 0, 0, 170))
    pr.draw_line_ex((0, bar_h), (WIDTH, bar_h), 2.0, (70, 80, 95, 255))

    r = bar_h * 0.30
    pulse = 0.5 + 0.5 * math.sin(current_time * 4.0)

    for label, cx, align in ((left_label, WIDTH * 0.06, 1),
                             (right_label, WIDTH * 0.94, -1)):
        if not label:
            continue
        cy = bar_y + bar_h / 2
        # A physical-looking red button: dark base, lit top, thin rim.
        pr.draw_circle(int(cx), int(cy + r * 0.16), r, BTN_RED_DARK)
        lit = (int(BTN_RED[0]), int(BTN_RED[1] + 40 * pulse),
               int(BTN_RED[2] + 40 * pulse), 255)
        pr.draw_circle(int(cx), int(cy), r, lit)
        pr.draw_circle(int(cx - r * 0.28), int(cy - r * 0.30), r * 0.22,
                       (255, 255, 255, 90))
        # Measure the label and offset by HALF its width plus the button
        # radius, so the text sits fully clear of the glyph instead of being
        # centred on a point only one radius away and overlapping it.
        size = bar_h * 0.34
        spacing = size / 10.0
        tw = pr.measure_text_ex(font, label, float(size), spacing).x
        offset = r + WIDTH * 0.010 + tw / 2.0
        draw_centered_text(font, label, cx + align * offset, cy,
                           size, (245, 245, 245, 255), False)


def draw_finish_line(cx, top_y, bot_y, colour, closeness, label, font):
    """
    One end of the tug-of-war: the line the flag has to reach.

    Drawn as a chequered post in that player's colour rather than the plain red
    stroke it used to be, because the old version was the same colour for both
    ends and gave no clue whose goal it was or how close anyone was to it.
    `closeness` runs 0..1 and drives a glow, so the screen visibly tightens as
    someone approaches a win.
    """
    h = bot_y - top_y
    w = max(6.0, h * 0.09)

    # Danger glow behind the post, growing as the flag closes in.
    if closeness > 0.05:
        g = int(150 * closeness)
        for i in (3, 2, 1):
            pr.draw_rectangle(int(cx - w * i * 0.9), int(top_y - h * 0.06 * i),
                              int(w * i * 1.8), int(h + h * 0.12 * i),
                              (colour[0], colour[1], colour[2], int(g / (i * 2.2))))

    # Chequered post: eight alternating blocks, colour against white.
    n = 8
    bh = h / n
    for i in range(n):
        c = colour if (i % 2 == 0) else (245, 245, 245, 255)
        pr.draw_rectangle(int(cx - w / 2), int(top_y + i * bh), int(w), int(bh + 1), c)
    pr.draw_rectangle_lines(int(cx - w / 2), int(top_y), int(w), int(h), (15, 18, 25, 255))

    if label:
        draw_centered_text(font, label, cx, top_y - h * 0.16, h * 0.24, colour)


SURGE_NONE, SURGE_ARMED, SURGE_FIRED = 0, 1, 2
# Must match SURGE_BONUS in the firmware - used only for the popup text.
SURGE_BONUS = 15
SURGE_VIOLET = (190, 120, 255, 255)


def draw_surge_prompt(font, cx, cy, size, current_time):
    """
    "DOUBLE TAP" nudge, shown only while a surge is actually armed.

    Kept small and put right under the multiplier badge, because that badge is
    already where the player looks when they catch fire. A prompt somewhere
    else on screen would simply not be seen mid-round.
    """
    pulse = 0.5 + 0.5 * math.sin(current_time * 7.0)
    a = int(160 + 95 * pulse)
    scale = 1.0 + 0.07 * pulse

    # Two chevrons standing in for the two taps.
    for i, dx in enumerate((-size * 1.25, -size * 0.85)):
        pr.draw_circle(int(cx + dx), int(cy), size * 0.20 * scale,
                       (SURGE_VIOLET[0], SURGE_VIOLET[1], SURGE_VIOLET[2], a))
    draw_centered_text(font, "DOUBLE TAP!", cx + size * 0.35, cy,
                       size * 0.72 * scale,
                       (SURGE_VIOLET[0], SURGE_VIOLET[1], SURGE_VIOLET[2], a))


def draw_surge_banner(font, cx, cy, w, h, current_time):
    """The big 'POWER SURGE!' hit, shown for the ~0.4s the board reports it."""
    pulse = 0.5 + 0.5 * math.sin(current_time * 20.0)
    glow = (SURGE_VIOLET[0], SURGE_VIOLET[1], SURGE_VIOLET[2], int(90 + 90 * pulse))
    pr.draw_rectangle_rounded((cx - w / 2 - 6, cy - h / 2 - 6, w + 12, h + 12), 0.4, 8, glow)
    pr.draw_rectangle_rounded((cx - w / 2, cy - h / 2, w, h), 0.4, 8, (30, 10, 50, 235))
    draw_centered_text(font, "POWER SURGE!", cx, cy, h * 0.58,
                       (255, 255, 255, 255))


def draw_ui(WIDTH, HEIGHT, fonts, game_data, calib_timers, ui_assets, MAX_SCORE_DIFF,
            fx=None):
    """
    The main mega-function that draws every frame of the game.
    """
        # 1. Unpack all our assets from the dictionary passed in     test_image = ui_assets["test"]    tug_of_war_gif = ui_assets["tug of war gif"]
    background_gif = ui_assets["bg"]
   
    player_1_gif = ui_assets["p1_gif"]
    player_2_gif = ui_assets["p2_gif"]
   
    easy_bot_gif = ui_assets["easy bot"]
    medium_bot_gif = ui_assets["medium bot"]
    hard_bot_gif = ui_assets["hard bot"]
   
    flag_texture = ui_assets["flag"]
    rope_texture = ui_assets["rope"]
    score_effect_texture = ui_assets["score_effect"]
    solo_texture = ui_assets["solo"]
   
 
    # 2. Extract current game status and fonts
    state = game_data["game_state"]
    gamemode = game_data["gamemode"]
    font_super_large, font_large, font_medium, font_small, main_font = fonts
    current_time = time.time()


    # 3. Calculate game logic values
    current_tick = game_data.get("current_tick", 0)
    # Level rises every 600 ticks, but capped: get_target_value has no behaviour
    # past MAX_LEVEL, so counting higher would just be a number that lies.
    level = min(current_tick // 600, MAX_LEVEL)
    target_float = get_target_value(current_tick, level)
    target_pos_100 = int(target_float * 100) # Convert 0.0-1.0 to 0-100 to match player pos


    # Determine if players are currently hitting the target area.
    # The board decides this for real (it's what actually scores), so use its
    # answer when we have it and only fall back to our own copy of the maths
    # when simulating - the two can disagree by a hair right on the boundary.
    HIT_TOLERANCE = 12 # Player must be within +/- 12 points of the target to score
    p1_hit = game_data.get("p1_hit")
    p2_hit = game_data.get("p2_hit")

    p1_is_scoring = p1_hit if p1_hit is not None else (
        abs(game_data.get("p1_pos", 50) - target_pos_100) <= HIT_TOLERANCE)
    p2_is_scoring = p2_hit if p2_hit is not None else (
        abs(game_data.get("p2_pos", 50) - target_pos_100) <= HIT_TOLERANCE)

    # Publish the hit flags so main.py can watch them for hot-streak sound effects
    game_data["p1_is_scoring"] = p1_is_scoring
    game_data["p2_is_scoring"] = p2_is_scoring

    # Armband health. If the Pi cannot hear the mainboard at all, every armband
    # is unknown rather than fine - say so instead of showing a confident zero.
    # NOTE: named *_health, not *_status - the CALIBRATION block below already
    # uses p1_status/p2_status for its "READY"/"WAITING" label strings, and the
    # two would silently shadow each other.
    p1_grace = game_data.get("p1_grace", 0)
    p2_grace = game_data.get("p2_grace", 0)

    p1_motion = game_data.get("p1_motion", 0)
    p2_motion = game_data.get("p2_motion", 0)

    p1_surge = game_data.get("p1_surge", SURGE_NONE)
    p2_surge = game_data.get("p2_surge", SURGE_NONE)

    link_ok = game_data.get("link_ok", True)
    p1_health = game_data.get("p1_status", STAT_OK)
    p2_health = game_data.get("p2_status", STAT_OK)
    if not link_ok:
        p1_health = STAT_NO_LINK
        p2_health = STAT_NO_LINK if gamemode == 2 else STAT_OK


    # The solo-mode clock starts when PLAYING begins. It MUST be cleared again
    # on the way out, otherwise the second solo run of the session reads a stale
    # timestamp and shows TIME: 0 the instant it starts.
    if state != GameState.GAME_OVER:
        calib_timers["win_fx_done"] = False
    if state != GameState.START_SCREEN:
        # Leaving the title resets the attract clock, so the demo always starts
        # from panel 1 the next time the cabinet is left alone.
        calib_timers["title_since"] = 0

    # Remember this frame's scores so the next frame can tell what changed.
    # Kept here (not in the PLAYING branch) so the baseline stays correct
    # across state changes and a new round never opens with a phantom "+250".
    if state != GameState.PLAYING:
        calib_timers["prev_p1_score"] = game_data["p1_score"]
        calib_timers["prev_p2_score"] = game_data["p2_score"]

    # --- CALIBRATION TIMER LOGIC ---
    # Trigger the timer if the state JUST changed to 1 (from either 0 or 2)
    if game_data["p1_calib"] == 1 and calib_timers["p1_last_state"] != 1:
        calib_timers["p1_start_time"] = current_time
    calib_timers["p1_last_state"] = game_data["p1_calib"]


    if game_data["p2_calib"] == 1 and calib_timers["p2_last_state"] != 1:
        calib_timers["p2_start_time"] = current_time
    calib_timers["p2_last_state"] = game_data["p2_calib"]




    # =========================================================================
    # BASE UI: Drawn in almost all states (The GIFs, Players, and the Rope)
    # =========================================================================
    if state in [GameState.START_SCREEN, GameState.COUNTDOWN, GameState.PLAYING, GameState.GAME_OVER]:
       
        # Draw the animated waterfall background
        background_gif.update()
        background_gif.draw((0, 0))
       
        # Calculate dynamic sizes based on screen dimensions
        player_w = WIDTH * 0.225
        player_h = HEIGHT * 0.4
       
        # --- PLAYER 1 CALCS (Left side) ---
        leftplayer_pos_x = 0.1
        leftplayer_pos_y = 0.6
       
        p1_tl_x = WIDTH * leftplayer_pos_x
        p1_tl_y = (HEIGHT * leftplayer_pos_y) - (player_h / 2)
        p1_center_x = p1_tl_x + player_w / 2
        p1_bottom = p1_tl_y + player_h
       
        # Draw Image instead of Rectangle for Player 1
        if p1_is_scoring and state == GameState.PLAYING:
            source_rect = pr.Rectangle(0, 0, score_effect_texture.width, score_effect_texture.height)
            dest_rect = pr.Rectangle(p1_tl_x, p1_tl_y, player_w, player_h)
            origin = pr.Vector2(0, 0)
            pr.draw_texture_pro(score_effect_texture, source_rect, dest_rect, origin, 0.0, WHITE)


        # --- PLAYER 2 CALCS (Right side) ---
        p2_tl_x = WIDTH * (1 - leftplayer_pos_x) - player_w
        p2_tl_y = (HEIGHT * leftplayer_pos_y) - (player_h / 2)
        p2_center_x = p2_tl_x + player_w / 2
        p2_bottom = p2_tl_y + player_h
       
        # Draw Image instead of Rectangle for Player 2
        if p2_is_scoring and state == GameState.PLAYING and  (gamemode == 2):  # Only show scoring effect for 2 player mode
            source_rect = pr.Rectangle(0, 0, score_effect_texture.width, score_effect_texture.height)
            dest_rect = pr.Rectangle(p2_tl_x, p2_tl_y, player_w, player_h)
            origin = pr.Vector2(0, 0)
            pr.draw_texture_pro(score_effect_texture, source_rect, dest_rect, origin, 0.0, WHITE)
           
        # Determine Name of Player 2 based on game mode and difficulty
        diff = game_data.get("difficulty", 1)
        if gamemode == 1:
            p2_name = "CPU (EASY)" if diff == 1 else "CPU (MED)" if diff == 2 else "CPU (HARD)" if diff == 3 else f"CPU (LVL {diff})"
        else:
            p2_name = "P2"
           
       
        # ==========================================
        # TEMPLE PLATFORM (Extends to the bottom of the screen)
        # ==========================================
        platform_padding = WIDTH * 0.02
        platform_x = p1_tl_x - platform_padding
        platform_w = (WIDTH * 0.8) + (platform_padding * 2)
        platform_y = p1_bottom - (HEIGHT * 0.04)
        platform_h = HEIGHT - platform_y + 50
       
        # 1. BASE LAYER
        pr.draw_rectangle_rounded((platform_x, platform_y, platform_w, platform_h), 0.05, 10, (30, 35, 45, 255))
       
        # 2. INNER LAYER
        inner_x = platform_x + (WIDTH * 0.01)
        inner_w = platform_w - (WIDTH * 0.02)
        pr.draw_rectangle_rounded((inner_x, platform_y, inner_w, platform_h), 0.05, 10, (55, 60, 70, 255))


        # 3. MOSS / ACCENT STRIPE
        stripe_y = platform_y + (HEIGHT * 0.015)
        stripe_h = HEIGHT * 0.02
        pr.draw_rectangle(int(inner_x), int(stripe_y), int(inner_w), int(stripe_h), (60, 120, 80, 255))
       
        # 4. GOLD/BRONZE TRIM
        gold_y = stripe_y + stripe_h
        pr.draw_rectangle(int(inner_x), int(gold_y), int(inner_w), int(HEIGHT * 0.005), (180, 140, 50, 255))


        # 5. TOP HIGHLIGHT
        pr.draw_rectangle_rounded((inner_x, platform_y, inner_w, HEIGHT * 0.01), 0.5, 10, (100, 110, 120, 255))
       
        # 6. STONE GROOVES
        for i in range(1, 10):
            line_x = inner_x + (inner_w * (i / 10.0))
            pr.draw_rectangle(int(line_x - 3), int(gold_y + 10), 6, int(platform_h), (30, 35, 45, 150))
       
   
        # Draw the player scores below their respective characters
        draw_centered_text(main_font, f"P1: {game_data['p1_score']}", p1_center_x, p1_bottom + HEIGHT * 0.05, font_small, WHITE)
        if gamemode != 3:
            draw_centered_text(main_font, f"{p2_name}: {game_data['p2_score']}", p2_center_x, p2_bottom + HEIGHT * 0.05, font_small, WHITE)
       
       
        # ==========================================
        # DRAW ROPE AND PLAYERS
        # ==========================================
        rope_start_pos_x = p1_tl_x + player_w
        rope_end_pos_x = p2_tl_x
        rope_width = rope_end_pos_x - rope_start_pos_x
       
        # 1. Draw the static Rope Image FIRST (so it sits behind the players' hands)
        # Height is set to player_h to perfectly match the height of your player GIFs
        source_rect = pr.Rectangle(0, 0, rope_texture.width, rope_texture.height)
        dest_rect = pr.Rectangle(rope_start_pos_x, p1_tl_y, rope_width, player_h)
        origin = pr.Vector2(0, 0)
        pr.draw_texture_pro(rope_texture, source_rect, dest_rect, origin, 0.0, WHITE)


       




# --- THE FLAG LOGIC ---
        rope_y = p2_tl_y + player_h / 2 - (player_h / 10)
       
        score_diff = game_data["p1_score"] - game_data["p2_score"]
        pull_percentage = max(-1.0, min(1.0, score_diff / MAX_SCORE_DIFF))
       
        rope_center_x = WIDTH / 2
        flag_w = WIDTH * 0.04
        flag_h = HEIGHT * 0.08
        padding = flag_w / 2
       
        max_travel = ((rope_end_pos_x - rope_start_pos_x) / 2) - padding
        flag_x = rope_center_x - (pull_percentage * max_travel)
           
        marker_h = HEIGHT * 0.1
        
        if gamemode != 3:
            left_win_x = rope_center_x - max_travel
            right_win_x = rope_center_x + max_travel

            # How close is the flag to each end? Drives the danger glow, so the
            # screen visibly tightens as someone approaches a win. The flag is
            # pulled LEFT when P1 leads, so the left post is P1's goal.
            p1_close = max(0.0, pull_percentage)        # +1 = P1 about to win
            p2_close = max(0.0, -pull_percentage)

            draw_finish_line(left_win_x, rope_y - marker_h * 0.75,
                             rope_y + marker_h * 0.75, RED, p1_close,
                             "P1 WINS", main_font)
            p2_label = "P2 WINS" if gamemode == 2 else "CPU WINS"
            draw_finish_line(right_win_x, rope_y - marker_h * 0.75,
                             rope_y + marker_h * 0.75, BLUE, p2_close,
                             p2_label, main_font)

            # A faint track between the posts so the flag's travel is legible
            # even before it has moved far from centre.
            pr.draw_line_ex((left_win_x, rope_y), (right_win_x, rope_y),
                            2.0, (90, 100, 115, 140))
           
            # 2. Draw the Custom Flag Image over the rope
            source_rect = pr.Rectangle(0, 0, flag_texture.width, flag_texture.height)
            dest_rect = pr.Rectangle(flag_x, rope_y + flag_h/3, flag_w, flag_h)
            origin = pr.Vector2(flag_w / 2, flag_h / 2)
           
            pr.draw_texture_pro(flag_texture, source_rect, dest_rect, origin, 0.0, WHITE)
       
        # Neutral centre line, dashed so it reads as a start marker rather than
        # a third goal post.
        seg = marker_h / 9.0
        for i in range(0, 9, 2):
            y0 = rope_y - marker_h / 2 + i * seg
            pr.draw_line_ex((rope_center_x, y0), (rope_center_x, y0 + seg), 3.0, GRAY)       
       
        # 2. Draw Player 1
        player_1_gif.update()
        player_1_gif.draw((p1_tl_x, p1_tl_y))
       
        # 3. Draw Player 2
        # (Futureproofing: if gamemode == 1, you can eventually draw bot_gif here instead)
        if gamemode == 1:
            diff = game_data.get("difficulty", 1)
            if diff == 1:
                easy_bot_gif.update()
                easy_bot_gif.draw((p2_tl_x, p2_tl_y))
            elif diff == 2:
                medium_bot_gif.update()
                medium_bot_gif.draw((p2_tl_x, p2_tl_y))
            elif diff == 3:
                hard_bot_gif.update()
                hard_bot_gif.draw((p2_tl_x, p2_tl_y))
            else:
                player_2_gif.update()
                player_2_gif.draw((p2_tl_x, p2_tl_y))
               
        elif gamemode == 2:
            player_2_gif.update()
            player_2_gif.draw((p2_tl_x, p2_tl_y))
        elif gamemode == 3:
            # Draw the static target image instead of the character
            source_rect = pr.Rectangle(0, 0, solo_texture.width, solo_texture.height)
            dest_rect = pr.Rectangle(p2_tl_x, p2_tl_y, player_w, player_h)
            origin = pr.Vector2(0, 0)
            pr.draw_texture_pro(solo_texture, source_rect, dest_rect, origin, 0.0, WHITE)




    # =========================================================================
    # STATE-SPECIFIC UI OVERLAYS
    # =========================================================================
    # match is like a switch statement, it runs only the block matching the current state
    match state:
        case GameState.START_SCREEN:
            # Draw a dark, semi-transparent rectangle over the background to make text pop
            pr.draw_rectangle(0, 0, WIDTH, HEIGHT, (0, 0, 0, 180))

            # How long have we been sitting on this screen untouched? Any state
            # change resets it, so the demo only runs when the cabinet is idle.
            if calib_timers.get("title_since", 0) == 0:
                calib_timers["title_since"] = current_time
            idle = current_time - calib_timers["title_since"]

            # Title pulses gently so the screen is never completely static.
            title_scale = 1.0 + 0.02 * math.sin(current_time * 2.0)
            draw_centered_text(main_font, "FLEX OFF!", WIDTH/2, HEIGHT * 0.3,
                               font_large * title_scale, WHITE)

            # After a few idle seconds the attract loop takes over and teaches
            # the game. It draws its own prompt, so ours is suppressed while on.
            showing_demo = attract.draw_attract(main_font, WIDTH, HEIGHT, idle,
                                                draw_centered_text,
                                                ui_assets.get("hiscores"))

            if not showing_demo and int(current_time * 2) % 2 == 0:
                draw_centered_text(main_font, "PRESS EITHER RED BUTTON", WIDTH/2, HEIGHT * 0.6, font_medium, YELLOW)


        case GameState.SELECTION:
            pr.draw_rectangle(0, 0, WIDTH, HEIGHT, (0, 0, 0, 210))
            draw_centered_text(main_font, "SELECT GAME MODE", WIDTH/2, HEIGHT * 0.15, font_large, YELLOW)


            # Set text variables based on what gamemode integer the ESP32 is currently sending
            if gamemode == 1: mode_text, desc_text = "1 PLAYER vs CPU", "Tug-of-war against an AI opponent!"
            elif gamemode == 2: mode_text, desc_text = "2 PLAYER PvP", "Head-to-head physical tug-of-war!"
            elif gamemode == 3: mode_text, desc_text = "SOLO TIMED RUSH", "Solo mode. Score as high as possible before time runs out!"
            elif gamemode == 4: mode_text, desc_text = "HIGH SCORES", "See who holds the cabinet records."
            else: mode_text, desc_text = "UNKNOWN MODE", ""


            # Draw the UI Menu
            draw_centered_text(main_font, "<", WIDTH * 0.2, HEIGHT * 0.5, font_large, GRAY)
            draw_centered_text(main_font, mode_text, WIDTH/2, HEIGHT * 0.5, font_large, WHITE)
            draw_centered_text(main_font, ">", WIDTH * 0.8, HEIGHT * 0.5, font_large, GRAY)
            draw_centered_text(main_font, desc_text, WIDTH/2, HEIGHT * 0.65, font_small, BLUE_BRIGHT)


            if int(current_time * 2) % 2 == 0:
                draw_centered_text(main_font, "or double-tap your armband to confirm", WIDTH/2, HEIGHT * 0.80, font_small * 0.9, GRAY, False)
           
        case GameState.CALIBRATION:
            pr.draw_rectangle(0, 0, WIDTH, HEIGHT, (0, 0, 0, 210))
            draw_centered_text(main_font, "CALIBRATION PHASE", WIDTH/2, HEIGHT * 0.15, font_large, YELLOW)
           
            # --- PLAYER 1 STATUS ---
            p1_color, p1_status = (GREEN, "READY") if game_data["p1_calib"] == 2 else (YELLOW, "CALIBRATING") if game_data["p1_calib"] == 1 else (RED, "WAITING")
            draw_centered_text(main_font, f"Player 1: {p1_status}", WIDTH * 0.25, HEIGHT * 0.35, font_medium, p1_color)
           
            # --- PLAYER 2 / BOT / TARGET STATUS ---
            p2_color, p2_status = (GREEN, "READY") if game_data["p2_calib"] == 2 else (YELLOW, "CALIBRATING") if game_data["p2_calib"] == 1 else (RED, "WAITING")
           
            # Rename Player 2 based on the Gamemode
            if gamemode == 1:
                draw_centered_text(main_font, f"CPU (Bot): {p2_status}", WIDTH * 0.75, HEIGHT * 0.35, font_medium, p2_color)
            elif gamemode == 3:
                draw_centered_text(main_font, f"TARGET: {p2_status}", WIDTH * 0.75, HEIGHT * 0.35, font_medium, p2_color)
            else:
                draw_centered_text(main_font, f"Player 2: {p2_status}", WIDTH * 0.75, HEIGHT * 0.35, font_medium, p2_color)




            # ==========================================
            # LIVE TEST GAUGES (Only show when READY)
            # ==========================================
            gauge_top = HEIGHT * 0.45
            gauge_bottom = HEIGHT * 0.8
            cursor_radius = float(HEIGHT * 0.02)


            # A practice target that moves exactly like the real one at level 0.
            # This is what makes the calibration screen a genuine preview: the
            # mapping from muscle to position is already identical to the game,
            # so adding the target means you can confirm you can actually REACH
            # both ends of the range before committing to it.
            practice_tick = current_time * 30.0          # the game runs at 30 Hz
            practice_pct = get_target_value(practice_tick, 0)
            practice_pos = practice_pct * 100.0

            def _gauge_y(pct):
                return gauge_top + ((gauge_bottom - gauge_top) * (1.0 - pct / 100.0))

            # Player 1 Live Gauge
            if game_data["p1_calib"] == 2:
                g1_x = WIDTH * 0.15
                pr.draw_line_ex((g1_x, gauge_top), (g1_x, gauge_bottom), 6.0, GRAY)

                # Band the target can roam within (it is clamped to 10..90), so
                # a player can see they never need the absolute extremes.
                band_top, band_bot = _gauge_y(90), _gauge_y(10)
                pr.draw_rectangle(int(g1_x - HEIGHT * 0.012), int(band_top),
                                  int(HEIGHT * 0.024), int(band_bot - band_top),
                                  (255, 255, 255, 18))

                # Cursor Y (inverted so 100 is at the top) - SAME formula the
                # highway uses during play.
                p1_pos_pct = game_data.get("p1_pos", 0) / 100.0
                p1_y = gauge_top + ((gauge_bottom - gauge_top) * (1.0 - p1_pos_pct))

                # Practice target + hit test, using the same tolerance as the game.
                t1_y = _gauge_y(practice_pos)
                on_t = abs(game_data.get("p1_pos", 0) - practice_pos) <= HIT_TOLERANCE
                pr.draw_line_ex((g1_x - HEIGHT * 0.030, t1_y), (g1_x + HEIGHT * 0.030, t1_y),
                                4.0, GREEN if on_t else WHITE)

                pr.draw_circle(int(g1_x), int(p1_y), cursor_radius,
                               GREEN if on_t else RED_BRIGHT)
                draw_centered_text(main_font, "MAX", g1_x, gauge_top - HEIGHT * 0.03, font_small, GRAY, False)
                draw_centered_text(main_font, "RELAX", g1_x, gauge_bottom + HEIGHT * 0.03, font_small, GRAY, False)


            # Player 2 Live Gauge (ONLY IN PVP MODE)
            if gamemode == 2 and game_data["p2_calib"] == 2:
                g2_x = WIDTH * 0.85
                pr.draw_line_ex((g2_x, gauge_top), (g2_x, gauge_bottom), 6.0, GRAY)

                band_top, band_bot = _gauge_y(90), _gauge_y(10)
                pr.draw_rectangle(int(g2_x - HEIGHT * 0.012), int(band_top),
                                  int(HEIGHT * 0.024), int(band_bot - band_top),
                                  (255, 255, 255, 18))

                p2_pos_pct = game_data.get("p2_pos", 0) / 100.0
                p2_y = gauge_top + ((gauge_bottom - gauge_top) * (1.0 - p2_pos_pct))

                t2_y = _gauge_y(practice_pos)
                on_t2 = abs(game_data.get("p2_pos", 0) - practice_pos) <= HIT_TOLERANCE
                pr.draw_line_ex((g2_x - HEIGHT * 0.030, t2_y), (g2_x + HEIGHT * 0.030, t2_y),
                                4.0, GREEN if on_t2 else WHITE)

                pr.draw_circle(int(g2_x), int(p2_y), cursor_radius,
                               GREEN if on_t2 else BLUE_BRIGHT)
                draw_centered_text(main_font, "MAX", g2_x, gauge_top - HEIGHT * 0.03, font_small, GRAY, False)
                draw_centered_text(main_font, "RELAX", g2_x, gauge_bottom + HEIGHT * 0.03, font_small, GRAY, False)




            # Figure out who is currently actively calibrating
            active_player = 0
            p1_elapsed = current_time - calib_timers["p1_start_time"] if calib_timers["p1_start_time"] > 0 else 0
            p2_elapsed = current_time - calib_timers["p2_start_time"] if calib_timers["p2_start_time"] > 0 else 0


            if game_data["p1_calib"] == 1: active_player, elapsed = 1, p1_elapsed
            elif game_data["p2_calib"] == 1: active_player, elapsed = 2, p2_elapsed


            if active_player == 0:
                # Fresh auto-range scale for whoever calibrates next.
                calib_timers["calib_peak"] = 1
                # Nobody is calibrating. Check if everyone required is Ready (State 2)
                p1_ready = (game_data["p1_calib"] == 2)
                p2_ready = (game_data["p2_calib"] == 2)
               
                is_ready_to_start = False
                if gamemode == 2 and p1_ready and p2_ready:
                    is_ready_to_start = True
                elif gamemode != 2 and p1_ready:
                    is_ready_to_start = True
               
                # Draw the instructions based on whether they are ready to start or not
                if is_ready_to_start:
                    draw_centered_text(main_font, "ALL PLAYERS READY!", WIDTH/2,
                                       HEIGHT * 0.52, font_large, GREEN)

                    # A real countdown, straight from the board. Without it the
                    # player stares at a "ready" screen with no idea whether
                    # anything is about to happen or whether it has hung.
                    cd = game_data.get("ready_cd", -1)
                    if cd >= 0:
                        urgent = cd <= 3
                        draw_centered_text(main_font, f"STARTING IN {cd}", WIDTH/2,
                                           HEIGHT * 0.655, font_large * 1.05,
                                           RED_BRIGHT if urgent else YELLOW)
                        # Drain bar, so the wait is legible at a glance.
                        bw, bh = WIDTH * 0.34, HEIGHT * 0.016
                        bx, by = WIDTH/2 - bw/2, HEIGHT * 0.735
                        pr.draw_rectangle(int(bx), int(by), int(bw), int(bh), (40, 46, 58, 255))
                        pr.draw_rectangle(int(bx), int(by), int(bw * (cd / 10.0)),
                                          int(bh), RED_BRIGHT if urgent else GREEN)
                        pr.draw_rectangle_lines(int(bx), int(by), int(bw), int(bh), GRAY)
                    else:
                        draw_centered_text(main_font, "GET READY", WIDTH/2,
                                           HEIGHT * 0.655, font_medium, WHITE)

                    draw_centered_text(main_font,
                                       "practise on the gauge - this is exactly how the game feels",
                                       WIDTH/2, HEIGHT * 0.855, font_small * 0.85, GRAY, False)
                    draw_centered_text(main_font,
                                       "press your button or double-tap to calibrate again",
                                       WIDTH/2, HEIGHT * 0.895, font_small * 0.85, GRAY, False)
                elif gamemode == 2:
                    draw_centered_text(main_font, "PRESS A RED BUTTON OR DOUBLE-TAP TO CALIBRATE", WIDTH/2, HEIGHT * 0.65, font_small, WHITE)
                else:
                    # 1-player modes: the first armband to calibrate claims the
                    # player slot, so make it clear either button will do.
                    draw_centered_text(main_font, "Press EITHER Calibration Button to Begin", WIDTH/2, HEIGHT * 0.65, font_small, WHITE)
                    draw_centered_text(main_font, "First to calibrate becomes the player", WIDTH/2, HEIGHT * 0.73, font_small, GRAY, False)
            else:
                # Someone is calibrating!
                # First 3 seconds = Relax. Next 3 seconds = Flex hard.
                if elapsed <= 3.0:
                    calib_phase, calib_progress = "RELAX", elapsed
                else:
                    # We subtract 3 so the progress bar resets to 0 for the "Flex" phase
                    calib_phase, calib_progress = "FLEX", min(elapsed - 3.0, 3.0)


                phase_text = "FLEX HARD!" if calib_phase == "FLEX" else "RELAX COMPLETELY..."
                phase_color = RED if calib_phase == "FLEX" else BLUE_BRIGHT
               
                draw_centered_text(main_font, f"PLAYER {active_player} CALIBRATING...", WIDTH/2, HEIGHT * 0.55, font_medium, WHITE)
                draw_centered_text(main_font, phase_text, WIDTH/2, HEIGHT * 0.7, font_large, phase_color)

                # ---- LIVE SIGNAL BAR -------------------------------------
                # Position is not meaningful yet (there is no min/max to map
                # against until calibration finishes), so this shows the raw
                # envelope instead. Without it the player spends six seconds
                # with no evidence the machine can see them at all, which is
                # the single most common "is it broken?" moment in the demo.
                env_key = "p1_env" if active_player == 1 else "p2_env"
                env_val = game_data.get(env_key, 0)

                # Auto-ranging: we cannot know this person's scale in advance,
                # so track the largest value seen this calibration and show
                # everything relative to it. Decays slowly so one noise spike
                # does not flatten the bar for the rest of the session.
                peak_key = "calib_peak"
                peak = max(calib_timers.get(peak_key, 1), env_val, 1)
                calib_timers[peak_key] = peak * 0.999
                frac = max(0.0, min(1.0, env_val / float(peak)))

                sig_w, sig_h = WIDTH * 0.5, HEIGHT * 0.045
                sig_x, sig_y = WIDTH / 2 - sig_w / 2, HEIGHT * 0.78

                pr.draw_rectangle_rounded((sig_x, sig_y, sig_w, sig_h), 0.4, 8, (0, 0, 0, 190))
                if frac > 0.01:
                    # Green while relaxed, warming to red as they push - the
                    # colour alone tells them which phase they are doing well.
                    bar_col = (int(60 + 195 * frac), int(230 - 130 * frac), 70, 255)
                    pr.draw_rectangle_rounded((sig_x + 3, sig_y + 3,
                                               (sig_w - 6) * frac, sig_h - 6),
                                              0.4, 8, bar_col)
                pr.draw_rectangle_rounded_lines((sig_x, sig_y, sig_w, sig_h), 0.4, 8, GRAY)
                draw_centered_text(main_font, "LIVE MUSCLE SIGNAL", WIDTH / 2,
                                   sig_y - HEIGHT * 0.032, font_small * 0.8, GRAY, False)


                # Draw the visual Progress Bar
                bar_w, bar_h = int(WIDTH * 0.4), int(HEIGHT * 0.05)
                bar_left = int(WIDTH/2 - bar_w/2)
                bar_top = int(HEIGHT * 0.85 - bar_h/2)
               
                pr.draw_rectangle_lines(bar_left, bar_top, bar_w, bar_h, GRAY)
               
                # Fill the bar based on time elapsed in current phase out of 3.0 seconds
                fill_w = int(max(0, min(bar_w, bar_w * (calib_progress / 3.0))))
                pr.draw_rectangle(bar_left, bar_top, fill_w, bar_h, phase_color)                
               


        case GameState.COUNTDOWN:
            # First time we enter this state, record the start time
            if calib_timers.get("countdown_start", 0) == 0:
                calib_timers["countdown_start"] = current_time
               
            time_in_countdown = current_time - calib_timers["countdown_start"]
            pr.draw_rectangle(0, 0, WIDTH, HEIGHT, (0, 0, 0, 180))
           
            # Each number punches in and settles, instead of appearing flat.
            # The eased scale is what makes a countdown feel like a countdown.
            if time_in_countdown < 1.0:
                text_str, tint = "3", YELLOW
            elif time_in_countdown < 2.0:
                text_str, tint = "2", YELLOW
            elif time_in_countdown < 3.0:
                text_str, tint = "1", YELLOW
            else:
                # The board holds COUNTDOWN for 4 s, so GO gets a full beat.
                text_str, tint = "GO!", GREEN

            step_t = time_in_countdown % 1.0
            # ease_out_back overshoots slightly then settles - a "landing" feel.
            c1, c3 = 1.70158, 2.70158
            e = 1 + c3 * pow(step_t - 1, 3) + c1 * pow(step_t - 1, 2)
            scale = 0.55 + 0.45 * min(1.0, e)

            # Faint expanding ring behind the number, like a shockwave.
            ring_r = HEIGHT * 0.12 * (0.4 + step_t * 1.6)
            ring_a = int(120 * max(0.0, 1.0 - step_t))
            pr.draw_circle_lines(int(WIDTH/2), int(HEIGHT/2), ring_r,
                                 (tint[0], tint[1], tint[2], ring_a))

            draw_centered_text(main_font, text_str, WIDTH/2, HEIGHT/2,
                               font_super_large * scale, tint)
           
        case GameState.PLAYING:
            calib_timers["countdown_start"] = 0 # Reset countdown timer for next match
           
            # Define the "Highway" (the vertical area where the tracking line moves)
            highway_bottom = p1_tl_y - (HEIGHT * 0.02) # Sits just above the player sprite
            highway_top = HEIGHT * 0.15
            highway_h = highway_bottom - highway_top
           
            scroll_speed = 0.2
            cursor_radius = float(HEIGHT * 0.02)


            # ==========================================
            # PLAYER 1 HIGHWAY
            # ==========================================
            p1_hw_w = player_w
            p1_hw_x = p1_tl_x
           
            # The "Hit X" is the vertical line where the player's cursor sits.
            # It's located 20% into the highway width from the left.
            p1_hit_x = int(p1_hw_x + (p1_hw_w * 0.2))
           
            # If P1 is on a hot streak, set their whole highway alight first
            p1_mult = game_data.get("p1_mult", 1)
            if p1_mult > 1:
                draw_fire_glow(p1_hw_x, highway_top, p1_hw_w, highway_h, current_time)

            # Draw highway background
            pr.draw_rectangle_rounded((p1_hw_x, highway_top, p1_hw_w, highway_h), 0.2, 10, (0, 0, 0, 160))
           
            p1_points = []
            # Loop through X coordinates from left to right, stepping by 5 pixels.
            # For every X pixel, we calculate what the sine wave *will* be in the future,
            # creating a preview of the upcoming wave.
            for x in range(int(p1_hw_x), int(p1_hw_x + p1_hw_w), 5):
                # How far into the future is this pixel?
                future_tick = current_tick + ((x - p1_hit_x) * scroll_speed)
                t_val = get_target_value(future_tick, level)
               
                # Convert the 0.0 - 1.0 target value into an actual Y pixel coordinate
                y = highway_top + (highway_h * (1.0 - t_val))
                p1_points.append((x, y))
               
            # Connect all our calculated points to draw a continuous wavy line
            for i in range(len(p1_points) - 1):
                pr.draw_line_ex(p1_points[i], p1_points[i+1], 6.0, WHITE)
           
            # Draw the vertical gray "Hit Line"
            pr.draw_line_ex((p1_hit_x, highway_top), (p1_hit_x, highway_bottom), 4.0, GRAY)
           
            # Calculate where the Player's actual physical cursor is vertically
            p1_pos_pct = game_data.get("p1_pos", 50) / 100.0
            p1_y = highway_top + (highway_h * (1.0 - p1_pos_pct))
           
            # Draw the player's cursor. Glows brighter if they are on target!
            c1_color = FIRE_ORANGE if p1_mult > 1 else (RED_BRIGHT if p1_is_scoring else RED)

            # A soft halo while on target. Two cheap circles read as a glow and
            # give constant, frame-by-frame feedback that you are scoring -
            # which matters more than the number, because the player's eyes are
            # locked on the cursor and never on the scoreboard.
            draw_surge_grace(p1_hit_x, p1_y, cursor_radius, p1_grace, current_time)
            if p1_is_scoring:
                pr.draw_circle(p1_hit_x, int(p1_y), cursor_radius * 2.1,
                               (c1_color[0], c1_color[1], c1_color[2], 45))
                pr.draw_circle(p1_hit_x, int(p1_y), cursor_radius * 1.5,
                               (c1_color[0], c1_color[1], c1_color[2], 80))
            pr.draw_circle(p1_hit_x, int(p1_y), cursor_radius, c1_color)

            if fx is not None:
                # Sparks are rate-limited rather than per-frame: at 60 fps a
                # continuous stream would drown the screen and the particle cap.
                if p1_is_scoring and int(current_time * 12) % 2 == 0:
                    fx.sparks(p1_hit_x, p1_y, c1_color, n=4)
                # Floating "+N" whenever the score actually moves. Driven off
                # the score itself, so it can never disagree with the board.
                d1 = game_data["p1_score"] - calib_timers.get("prev_p1_score", game_data["p1_score"])
                if d1 > 0:
                    fx.popup(p1_hit_x, p1_y - cursor_radius * 2, f"+{d1}",
                             FIRE_YELLOW if p1_mult > 1 else (255, 235, 140, 255))

            # P1's multiplier badge, sitting in the gap above the highway
            if p1_mult > 1:
                draw_multiplier_badge(main_font, p1_hw_x + p1_hw_w / 2, HEIGHT * 0.075,
                                      p1_hw_w * 0.85, HEIGHT * 0.11, p1_mult, current_time)

            # Surge prompt tucked directly under the badge - the one place the
            # player is already looking the instant they catch fire.
            if p1_surge == SURGE_ARMED:
                draw_surge_prompt(main_font, p1_hw_x + p1_hw_w / 2,
                                  HEIGHT * 0.145, HEIGHT * 0.048, current_time)


            # ==========================================
            # PLAYER 2 HIGHWAY (Only drawn if PvP Mode)
            # ==========================================
            # The math here is entirely identical to P1, just shifted to the right side of the screen.
            if gamemode != 3:
                p2_hw_w = player_w
                p2_hw_x = p2_tl_x
                p2_hit_x = int(p2_hw_x + (p2_hw_w * 0.2))

                p2_mult = game_data.get("p2_mult", 1)
                if p2_mult > 1:
                    draw_fire_glow(p2_hw_x, highway_top, p2_hw_w, highway_h, current_time)

                pr.draw_rectangle_rounded((p2_hw_x, highway_top, p2_hw_w, highway_h), 0.2, 10, (0, 0, 0, 160))
               
                p2_points = []
                for x in range(int(p2_hw_x), int(p2_hw_x + p2_hw_w), 5):
                    future_tick = current_tick + ((x - p2_hit_x) * scroll_speed)
                    t_val = get_target_value(future_tick, level)
                    y = highway_top + (highway_h * (1.0 - t_val))
                    p2_points.append((x, y))


                for i in range(len(p2_points) - 1):
                    pr.draw_line_ex(p2_points[i], p2_points[i+1], 6.0, WHITE)
               
                pr.draw_line_ex((p2_hit_x, highway_top), (p2_hit_x, highway_bottom), 4.0, GRAY)
               
                p2_pos_pct = game_data.get("p2_pos", 50) / 100.0
                p2_y = highway_top + (highway_h * (1.0 - p2_pos_pct))
                c2_color = FIRE_ORANGE if p2_mult > 1 else (BLUE_BRIGHT if p2_is_scoring else BLUE)

                draw_surge_grace(p2_hit_x, p2_y, cursor_radius, p2_grace, current_time)
                if p2_is_scoring:
                    pr.draw_circle(p2_hit_x, int(p2_y), cursor_radius * 2.1,
                                   (c2_color[0], c2_color[1], c2_color[2], 45))
                    pr.draw_circle(p2_hit_x, int(p2_y), cursor_radius * 1.5,
                                   (c2_color[0], c2_color[1], c2_color[2], 80))
                pr.draw_circle(p2_hit_x, int(p2_y), cursor_radius, c2_color)

                if fx is not None:
                    if p2_is_scoring and int(current_time * 12) % 2 == 0:
                        fx.sparks(p2_hit_x, p2_y, c2_color, n=4)
                    d2 = game_data["p2_score"] - calib_timers.get("prev_p2_score", game_data["p2_score"])
                    if d2 > 0:
                        fx.popup(p2_hit_x, p2_y - cursor_radius * 2, f"+{d2}",
                                 FIRE_YELLOW if p2_mult > 1 else (170, 220, 255, 255))

                if p2_mult > 1:
                    draw_multiplier_badge(main_font, p2_hw_x + p2_hw_w / 2, HEIGHT * 0.075,
                                          p2_hw_w * 0.85, HEIGHT * 0.11, p2_mult, current_time)

                if p2_surge == SURGE_ARMED:
                    draw_surge_prompt(main_font, p2_hw_x + p2_hw_w / 2,
                                      HEIGHT * 0.145, HEIGHT * 0.048, current_time)
           
       
           
            # ==========================================
            # TOP MIDDLE HUD (Changes based on Gamemode)
            # ==========================================
            if gamemode == 3:
                # 1. Start tracking time the moment PLAYING state begins
                # The board owns the clock. It sends the remaining seconds
                # outright, so the display cannot drift out of step with the
                # tick counter that actually ends the round. The old code ran
                # its own time.time() stopwatch against a hard-coded 60, which
                # disagreed with the board by however long the Pi took to
                # notice the state change.
                time_left = game_data.get("time_left", -1)
                if time_left < 0:
                    # Older firmware that does not send it: derive from the
                    # transmitted tick count rather than inventing a clock.
                    time_left = max(0, (1800 - game_data.get("current_tick", 0)) // 30)
               
                # Draw a dark, semi-transparent banner so the text is super readable
                banner_w = WIDTH * 0.3
                banner_h = HEIGHT * 0.16
                banner_x = int(WIDTH/2 - banner_w/2)
                pr.draw_rectangle_rounded((banner_x, -10, banner_w, banner_h), 0.2, 10, (0, 0, 0, 180))
                pr.draw_rectangle_lines_ex((banner_x, -10, banner_w, banner_h), 4, (100, 100, 100, 255)) # Gray border
               
                # Emphasize the Time Left
                draw_centered_text(main_font, f"TIME: {time_left}s", WIDTH/2, HEIGHT * 0.04, font_medium, YELLOW)
               
                # Emphasize the Score
                draw_centered_text(main_font, f"SCORE: {game_data['p1_score']}", WIDTH/2, HEIGHT * 0.11, font_medium, GREEN)
               
            else:
                # Standard Speed Level display for Mode 1 and 2
                draw_centered_text(main_font, f"Speed Level: {level}", WIDTH/2, HEIGHT * 0.06, font_small, YELLOW)


        case GameState.NAME_ENTRY:
            pr.draw_rectangle(0, 0, WIDTH, HEIGHT, (0, 0, 0, 215))

            draw_centered_text(main_font, "NEW HIGH SCORE!", WIDTH/2, HEIGHT * 0.20,
                               font_large, YELLOW)
            draw_centered_text(main_font, str(game_data["p1_score"]), WIDTH/2,
                               HEIGHT * 0.31, font_large * 1.3, GREEN)

            # The board rolls two indices; the words live in highscores.py.
            spun = highscores.name_from_seed(game_data.get("name_a", 0),
                                             game_data.get("name_b", 0))

            # Slot-machine style frame around the name.
            nw, nh = WIDTH * 0.56, HEIGHT * 0.13
            nx, ny = WIDTH/2 - nw/2, HEIGHT * 0.44
            glow = int(120 + 80 * (0.5 + 0.5 * math.sin(current_time * 5.0)))
            pr.draw_rectangle_rounded((nx - 6, ny - 6, nw + 12, nh + 12), 0.3, 8,
                                      (255, 200, 60, glow))
            pr.draw_rectangle_rounded((nx, ny, nw, nh), 0.3, 8, (18, 22, 36, 245))
            draw_centered_text(main_font, spun, WIDTH/2, ny + nh/2,
                               font_medium * 1.15, WHITE)

            draw_centered_text(main_font, "YOUR ARCADE NAME", WIDTH/2, HEIGHT * 0.40,
                               font_small * 0.85, GRAY, False)
            if int(current_time * 2) % 2 == 0:
                draw_centered_text(main_font, "LEFT = RESPIN     RIGHT = KEEP IT",
                                   WIDTH/2, HEIGHT * 0.66, font_small, WHITE)

        case GameState.HIGHSCORES:
            pr.draw_rectangle(0, 0, WIDTH, HEIGHT, (0, 0, 0, 215))
            entries = ui_assets.get("hiscores", [])
            draw_scoreboard(main_font, WIDTH, HEIGHT, entries,
                            HEIGHT * 0.28, HEIGHT * 0.072,
                            highlight=ui_assets.get("hiscore_new"))

        case GameState.GAME_OVER:
            pr.draw_rectangle(0, 0, WIDTH, HEIGHT, (0, 0, 0, 180))

            # Fire the celebration exactly once, on entry. Nobody is aiming at
            # anything on this screen, so this is the one place a screen kick
            # is safe (see the note at the top of juice.py).
            if fx is not None and not calib_timers.get("win_fx_done", False):
                calib_timers["win_fx_done"] = True
                human_won = (game_data["p1_score"] > game_data["p2_score"]) or gamemode == 3
                if human_won:
                    fx.confetti(WIDTH, [YELLOW, GREEN, BLUE_BRIGHT, RED_BRIGHT, WHITE])
                    fx.flash(WHITE, 0.35)
                    fx.kick(HEIGHT * 0.012)
                else:
                    fx.flash(RED, 0.30)
           
            p1_score = game_data["p1_score"]
            p2_score = game_data["p2_score"]
            current_diff = game_data.get("difficulty", 1)


            # Different Game Over screens based on the mode played
            if gamemode == 1: # Player vs CPU
                if p1_score > p2_score:
                    # If they beat the hardest bot
                    if current_diff >= 3:
                        draw_centered_text(main_font, "CHAMPION!", WIDTH/2, HEIGHT * 0.35, font_large, YELLOW)
                        draw_centered_text(main_font, "You Defeated the Hard Bot!", WIDTH/2, HEIGHT * 0.5, font_medium, GREEN)
                        if int(current_time * 2) % 2 == 0:
                            draw_centered_text(main_font, "LEFT = PLAY AGAIN     RIGHT = MAIN MENU", WIDTH/2, HEIGHT * 0.7, font_small, WHITE)
                    # If they beat a bot and need to advance to the next level
                    else:
                        next_diff = current_diff + 1
                        next_bot = "Medium Bot" if next_diff == 2 else "Hard Bot" if next_diff == 3 else f"Level {next_diff} Bot"


                        draw_centered_text(main_font, "STAGE CLEARED!", WIDTH/2, HEIGHT * 0.35, font_large, GREEN)
                        draw_centered_text(main_font, f"Next Opponent: {next_bot}", WIDTH/2, HEIGHT * 0.5, font_medium, BLUE_BRIGHT)
                        if int(current_time * 2) % 2 == 0:
                            draw_centered_text(main_font, "LEFT = PLAY AGAIN     RIGHT = MAIN MENU", WIDTH/2, HEIGHT * 0.7, font_small, WHITE)
                # Player lost to CPU
                else:
                    draw_centered_text(main_font, "CPU WINS...", WIDTH/2, HEIGHT * 0.4, font_large, RED)
                    if int(current_time * 2) % 2 == 0:
                        draw_centered_text(main_font, "LEFT = PLAY AGAIN     RIGHT = MAIN MENU", WIDTH/2, HEIGHT * 0.6, font_medium, WHITE)


            elif gamemode == 2: # Player Vs Player
                winner = "PLAYER 1 WINS!" if p1_score > p2_score else "PLAYER 2 WINS!"
                if p1_score == p2_score: winner = "IT'S A TIE!"


                draw_centered_text(main_font, winner, WIDTH/2, HEIGHT * 0.4, font_large, GREEN)
                if int(current_time * 2) % 2 == 0:
                    draw_centered_text(main_font, "LEFT = PLAY AGAIN     RIGHT = MAIN MENU", WIDTH/2, HEIGHT * 0.6, font_medium, WHITE)
                   
            elif gamemode == 3: # Timed Solo Rush
                draw_centered_text(main_font, "TIME UP!", WIDTH/2, HEIGHT * 0.35, font_large, YELLOW)
                draw_centered_text(main_font, f"Final Score: {p1_score}", WIDTH/2, HEIGHT * 0.5, font_medium, WHITE)
                if int(current_time * 2) % 2 == 0:
                    draw_centered_text(main_font, "LEFT = PLAY AGAIN     RIGHT = MAIN MENU", WIDTH/2, HEIGHT * 0.7, font_small, WHITE)


    # ---- POWER SURGE celebration --------------------------------------------
    # The board holds SURGE_FIRED for ~0.4s, so the banner rides that directly.
    # The one-shot effects are edge-triggered off it so they fire once, not
    # once per frame for the whole window.
    if state == GameState.PLAYING:
        # Read the multipliers from game_data rather than the locals: p2_mult is
        # only assigned inside the PvP branch, so touching it in solo or vs-CPU
        # raises UnboundLocalError.
        for tag, surge, mult, cx in (
                ("p1", p1_surge, game_data.get("p1_mult", 1), WIDTH * 0.25),
                ("p2", p2_surge, game_data.get("p2_mult", 1), WIDTH * 0.75)):
            if surge != SURGE_FIRED:
                calib_timers[tag + "_surge_fx"] = False
                continue
            draw_surge_banner(main_font, cx, HEIGHT * 0.44,
                              WIDTH * 0.26, HEIGHT * 0.085, current_time)
            if fx is not None and not calib_timers.get(tag + "_surge_fx", False):
                calib_timers[tag + "_surge_fx"] = True
                fx.flash(SURGE_VIOLET, 0.42)
                fx.sparks(cx, HEIGHT * 0.44, SURGE_VIOLET, n=34, speed=340)
                fx.popup(cx, HEIGHT * 0.52, f"+{SURGE_BONUS * max(1, mult)}", SURGE_VIOLET)

    # ---- effects bookkeeping -------------------------------------------------
    if state == GameState.PLAYING and fx is not None:
        # Streak ignition is the single biggest moment in a round, so it gets a
        # full-screen tint. Edge-triggered, so it fires once per ignition.
        for key, mult in (("prev_p1_mult_fx", game_data.get("p1_mult", 1)),
                          ("prev_p2_mult_fx", game_data.get("p2_mult", 1))):
            if mult > calib_timers.get(key, 1):
                fx.flash(FIRE_ORANGE, 0.30)
            calib_timers[key] = mult

        calib_timers["prev_p1_score"] = game_data["p1_score"]
        calib_timers["prev_p2_score"] = game_data["p2_score"]
    elif fx is not None:
        calib_timers["prev_p1_mult_fx"] = 1
        calib_timers["prev_p2_mult_fx"] = 1

    # =========================================================================
    # BUTTON LEGEND
    # =========================================================================
    # Two unlabelled red buttons on the panel, whose meaning changes per screen.
    # Showing the current meaning at the matching side of the display is what
    # keeps them obvious without printing anything on the cabinet itself.
    if state == GameState.START_SCREEN:
        legend = ("START", "START")
    elif state == GameState.SELECTION:
        legend = ("CHANGE MODE", "CONFIRM")
    elif state == GameState.CALIBRATION:
        if gamemode == 2:
            legend = ("CALIBRATE P1", "CALIBRATE P2")
        else:
            # In a 1-player mode either button claims the slot, so whichever
            # side the player is standing on works.
            legend = ("CALIBRATE ME", "CALIBRATE ME")
    elif state == GameState.NAME_ENTRY:
        legend = ("RESPIN", "KEEP IT")
    elif state == GameState.HIGHSCORES:
        legend = ("BACK", "BACK")
    elif state == GameState.GAME_OVER:
        # Beating a CPU bot promotes you up the ladder, so the left button is
        # labelled for what it will actually do.
        beat_bot = (gamemode == 1 and game_data["p1_score"] > game_data["p2_score"]
                    and game_data.get("difficulty", 1) < 3)
        legend = ("NEXT LEVEL" if beat_bot else "PLAY AGAIN", "MAIN MENU")
    else:
        # COUNTDOWN and PLAYING: buttons do nothing, so promise nothing.
        legend = (None, None)

    if legend != (None, None):
        draw_button_legend(main_font, WIDTH, HEIGHT, legend[0], legend[1], current_time)

    # =========================================================================
    # EMERGENCY RESET PROGRESS
    # =========================================================================
    # Both buttons held returns the cabinet to the title screen. Showing the
    # progress is what makes it usable: without it you are holding two buttons
    # for three seconds with no idea whether anything is happening.
    reset_hold = game_data.get("reset_hold", 0)
    if reset_hold > 0:
        f = reset_hold / 100.0
        pr.draw_rectangle(0, 0, int(WIDTH), int(HEIGHT), (0, 0, 0, int(150 * f)))
        draw_centered_text(main_font, "RESETTING...", WIDTH / 2, HEIGHT * 0.44,
                           font_large * (0.8 + 0.2 * f), RED_BRIGHT)
        bw, bh = WIDTH * 0.42, HEIGHT * 0.022
        bx, by = WIDTH / 2 - bw / 2, HEIGHT * 0.53
        pr.draw_rectangle(int(bx), int(by), int(bw), int(bh), (40, 46, 58, 255))
        pr.draw_rectangle(int(bx), int(by), int(bw * f), int(bh), RED_BRIGHT)
        pr.draw_rectangle_lines(int(bx), int(by), int(bw), int(bh), WHITE)
        draw_centered_text(main_font, "release to cancel", WIDTH / 2, HEIGHT * 0.585,
                           font_small * 0.9, GRAY, False)

    # =========================================================================
    # ELECTRODE / LINK ALERTS  (drawn last so nothing can cover them)
    # =========================================================================
    # Only shown in the states where a player is actually meant to be wearing
    # the armband. On the title and mode-select screens nobody has strapped in
    # yet, so a warning there would be noise rather than information.
    if state in (GameState.CALIBRATION, GameState.COUNTDOWN,
                 GameState.PLAYING, GameState.GAME_OVER):

        alert_w = WIDTH * 0.30
        alert_h = HEIGHT * 0.10
        alert_y = HEIGHT * 0.30

        # P1 lives in the left half, P2 in the right half.
        draw_status_alert(main_font, WIDTH * 0.25, alert_y, alert_w, alert_h,
                          p1_health, current_time, "P1")

        # Motion hint sits below the electrode alert. Suppressed when the
        # electrodes are already flagged, because a leads-off warning is the
        # more urgent problem and two stacked warnings is just noise.
        if p1_health == STAT_OK:
            draw_motion_warning(main_font, WIDTH * 0.25, alert_y + alert_h * 1.6,
                                alert_w * 0.85, alert_h * 0.8, p1_motion, current_time)

        # Only warn about P2 when P2 is a real person wearing a real armband.
        if gamemode == 2:
            draw_status_alert(main_font, WIDTH * 0.75, alert_y, alert_w, alert_h,
                              p2_health, current_time, "P2")
            if p2_health == STAT_OK:
                draw_motion_warning(main_font, WIDTH * 0.75, alert_y + alert_h * 1.6,
                                    alert_w * 0.85, alert_h * 0.8, p2_motion, current_time)
