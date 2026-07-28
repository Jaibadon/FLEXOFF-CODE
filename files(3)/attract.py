"""
attract.py - the idle "how to play" loop.

Why this exists
---------------
A judge walks up to the cabinet knowing nothing. Every second they spend
working out what the machine wants from them is a second of a short judging
slot burned, and confusion reads as a design flaw even when the hardware is
perfect. Real arcade cabinets solved this decades ago with an attract loop, so
Flex-Off has one: after a few idle seconds on the title screen it starts
cycling animated panels that teach the three things you need to know.

Everything here is drawn procedurally - no image files - so it cannot fail with
a missing-asset error on the day, and it costs nothing to the download size.
"""

import math
import pyray as pr


# How long the title screen sits still before the demo loop starts, and how
# long each panel holds. Slow enough to read, fast enough that a judge sees
# more than one panel while they are strapping electrodes on.
IDLE_BEFORE_ATTRACT = 6.0
PANEL_SECONDS = 5.0

INK = (240, 240, 240, 255)
DIM = (150, 160, 175, 255)
CYAN = (90, 220, 255, 255)
AMBER = (255, 190, 60, 255)
RED = (235, 70, 70, 255)
GREEN = (90, 230, 130, 255)


def _panel_frame(x, y, w, h):
    """Chunky bordered box, arcade cabinet style."""
    pr.draw_rectangle_rounded((x + 6, y + 6, w, h), 0.08, 8, (0, 0, 0, 170))
    pr.draw_rectangle_rounded((x, y, w, h), 0.08, 8, (18, 24, 40, 235))
    pr.draw_rectangle_rounded_lines((x, y, w, h), 0.08, 8, CYAN)


def _draw_arm(cx, cy, scale, flex_t):
    """
    A minimal bicep that bulges with flex_t (0 relaxed .. 1 flexed).
    Stick-figure deliberately: it reads instantly at a glance from two metres
    away, which a detailed sprite would not.
    """
    upper_len = 46 * scale
    fore_len = 42 * scale

    # Curls from a relaxed 30 degrees (down/right) up to -105 degrees
    ang = math.radians(30 - 135 * flex_t)
    shoulder = (cx - upper_len * 0.5, cy - 10 * scale)
    elbow = (shoulder[0] + upper_len, cy + 16 * scale)
    hand = (elbow[0] + math.cos(ang) * fore_len,
            elbow[1] + math.sin(ang) * fore_len)
    thick = 9 * scale
    pr.draw_line_ex(shoulder, elbow, thick, (225, 190, 160, 255))
    pr.draw_line_ex(elbow, hand, thick, (225, 190, 160, 255))

    # Bicep bulges upwards. The centre's Y is offset so the bottom of the
    # circle stays anchored to the bottom of the arm bone.
    bulge = (7 + 9 * flex_t) * scale
    bx = shoulder[0] + (elbow[0] - shoulder[0]) * 0.5
    base_y = shoulder[1] + (elbow[1] - shoulder[1]) * 0.5
    by = base_y - bulge + (4.5 * scale)

    col = (int(225 + 25 * flex_t), int(190 - 60 * flex_t), int(160 - 90 * flex_t), 255)
    pr.draw_circle(int(bx), int(by), bulge, col)

    # Electrode pads on the forearm, spaced along the bone.
    for t in (0.35, 0.65):
        ex = elbow[0] + math.cos(ang) * fore_len * t
        ey = elbow[1] + math.sin(ang) * fore_len * t
        pr.draw_circle(int(ex), int(ey), 4.0 * scale, CYAN)


def _panel_flex(font, x, y, w, h, t, draw_text):
    """Panel 1: flexing raises your cursor."""
    # 0 .. 1 .. 0 so the arm visibly pumps rather than snapping.
    flex = 0.5 - 0.5 * math.cos(t * 2.2)

    draw_text(font, "1. FLEX TO CLIMB", x + w / 2, y + h * 0.13, h * 0.14, AMBER)

    _draw_arm(x + w * 0.30, y + h * 0.55, h / 190.0, flex)

    # A vertical gauge that tracks the flex, so cause and effect are on screen
    # at the same time.
    gx = x + w * 0.66
    gtop, gbot = y + h * 0.32, y + h * 0.80
    pr.draw_rectangle_rounded((gx - h * 0.05, gtop, h * 0.10, gbot - gtop),
                              0.4, 8, (0, 0, 0, 170))
    fill_h = (gbot - gtop) * flex
    pr.draw_rectangle_rounded((gx - h * 0.045, gbot - fill_h, h * 0.09, fill_h),
                              0.4, 8, GREEN)
    cy = gbot - fill_h
    pr.draw_circle(int(gx), int(cy), h * 0.045, RED)

    draw_text(font, "RELAX = DOWN", x + w * 0.66, y + h * 0.88, h * 0.075, DIM)


def _panel_track(font, x, y, w, h, t, draw_text):
    """Panel 2: hold the cursor on the moving line."""
    draw_text(font, "2. STAY ON THE LINE", x + w / 2, y + h * 0.13, h * 0.14, AMBER)

    left, right = x + w * 0.10, x + w * 0.90
    mid_y = y + h * 0.55
    amp = h * 0.20

    # The scrolling target wave.
    pts = []
    for px in range(int(left), int(right), 6):
        phase = (px - left) * 0.022 - t * 2.4
        pts.append((px, mid_y + math.sin(phase) * amp))
    for i in range(len(pts) - 1):
        pr.draw_line_ex(pts[i], pts[i + 1], 5.0, INK)

    # The player's cursor, sitting on a fixed vertical line and tracking well.
    hit_x = left + (right - left) * 0.25
    phase = (hit_x - left) * 0.022 - t * 2.4
    ideal = mid_y + math.sin(phase) * amp
    # A deliberate small wobble: perfect tracking would imply it is easy.
    player_y = ideal + math.sin(t * 5.3) * h * 0.035
    on_target = abs(player_y - ideal) < h * 0.055

    pr.draw_line_ex((hit_x, y + h * 0.28), (hit_x, y + h * 0.82), 3.0, DIM)
    pr.draw_circle(int(hit_x), int(player_y), h * 0.05,
                   GREEN if on_target else RED)
    if on_target:
        draw_text(font, "+1", hit_x + w * 0.06, player_y - h * 0.10, h * 0.10, GREEN)

    draw_text(font, "ON TARGET = POINTS", x + w / 2, y + h * 0.90, h * 0.075, DIM)


def _panel_win(font, x, y, w, h, t, draw_text):
    """Panel 3: points drag the flag; drag it far enough and you win."""
    draw_text(font, "3. WIN THE TUG-OF-WAR", x + w / 2, y + h * 0.13, h * 0.14, AMBER)

    left, right = x + w * 0.14, x + w * 0.86
    rope_y = y + h * 0.55
    pr.draw_line_ex((left, rope_y), (right, rope_y), 6.0, (190, 165, 125, 255))

    # Win lines at each end.
    for ex, col in ((left, RED), (right, CYAN)):
        pr.draw_line_ex((ex, rope_y - h * 0.13), (ex, rope_y + h * 0.13), 5.0, col)

    # The flag drifts toward P1 to show a player winning.
    swing = math.sin(t * 1.5)
    fx = (left + right) / 2 - swing * (right - left) * 0.32
    pr.draw_line_ex((fx, rope_y), (fx, rope_y - h * 0.20), 4.0, INK)
    pr.draw_triangle((fx, rope_y - h * 0.20),
                     (fx, rope_y - h * 0.07),
                     (fx + w * 0.075, rope_y - h * 0.135), AMBER)
    pr.draw_circle(int(fx), int(rope_y), h * 0.035, INK)

    draw_text(font, "P1", left, rope_y + h * 0.24, h * 0.09, RED)
    draw_text(font, "P2", right, rope_y + h * 0.24, h * 0.09, CYAN)
    draw_text(font, "PULL THE FLAG TO YOUR SIDE", x + w / 2, y + h * 0.90, h * 0.075, DIM)


def _panel_scores(font, x, y, w, h, t, draw_text, entries=None):
    """Panel 4: the cabinet's current record holders."""
    draw_text(font, "4. CABINET RECORDS", x + w / 2, y + h * 0.13, h * 0.14, AMBER)

    entries = entries or []
    if not entries:
        draw_text(font, "NO SCORES YET", x + w / 2, y + h * 0.48, h * 0.13, DIM)
        draw_text(font, "play SOLO TIMED RUSH to be the first",
                  x + w / 2, y + h * 0.62, h * 0.075, DIM)
        return

    # Top five only - the panel is on screen for five seconds.
    show = entries[:5]
    row_h = h * 0.115
    top = y + h * 0.30
    for i, e in enumerate(show):
        ry = top + i * row_h
        col = AMBER if i == 0 else INK
        draw_text(font, "%d" % (i + 1), x + w * 0.16, ry, row_h * 0.62, DIM)
        draw_text(font, e["name"], x + w * 0.47, ry, row_h * 0.62, col)
        draw_text(font, str(e["score"]), x + w * 0.83, ry, row_h * 0.62, GREEN)


PANELS = (_panel_flex, _panel_track, _panel_win, _panel_scores)


def draw_attract(font, W, H, idle_seconds, draw_text, entries=None):
    """
    Draw the attract loop. Call this on the title screen only.

    `idle_seconds` is how long the title screen has been up untouched, and
    `draw_text` is UI.draw_centered_text passed in so the demo automatically
    uses the same font and faux-bold as the rest of the game.

    Returns True if it drew anything, so the caller can suppress the normal
    "press START" prompt while the demo is on screen.
    """
    if idle_seconds < IDLE_BEFORE_ATTRACT:
        return False

    t = idle_seconds - IDLE_BEFORE_ATTRACT
    index = int(t / PANEL_SECONDS) % len(PANELS)
    local = t % PANEL_SECONDS

    # Fade each panel in and out so the cycle does not strobe.
    fade = min(1.0, local / 0.45, (PANEL_SECONDS - local) / 0.45)
    fade = max(0.0, fade)

    pw, ph = W * 0.62, H * 0.52
    px, py = (W - pw) / 2, H * 0.26

    pr.draw_rectangle(0, 0, int(W), int(H), (0, 0, 0, int(150 * fade)))
    if fade < 0.05:
        return True

    _panel_frame(px, py, pw, ph)
    # Only the scores panel needs the leaderboard, so it is passed separately
    # rather than forcing every panel to take an argument it ignores.
    if PANELS[index] is _panel_scores:
        _panel_scores(font, px, py, pw, ph, t, draw_text, entries)
    else:
        PANELS[index](font, px, py, pw, ph, t, draw_text)

    # ---- "this is a demo, not a fault" ---------------------------------
    # A screen that animates by itself can read as a hang or a glitch if it is
    # not labelled. Three cues make the intent unmistakable: a blinking DEMO
    # tag in the corner (the arcade convention), a live progress bar for the
    # current panel so it is visibly counting rather than stuck, and a standing
    # "press start" prompt proving input is still being accepted.
    # Sits BELOW the button-legend bar that now runs along the top of the
    # screen (it occupies the first 8.5% of the height), so the two never
    # overlap on the title screen where both are drawn.
    tag_w, tag_h = W * 0.115, H * 0.042
    tag_x, tag_y = W * 0.028, H * 0.105
    if int(t * 1.6) % 2 == 0:
        pr.draw_rectangle_rounded((tag_x, tag_y, tag_w, tag_h), 0.35, 8, RED)
        draw_text(font, "DEMO", tag_x + tag_w / 2, tag_y + tag_h / 2,
                  tag_h * 0.62, (255, 255, 255, 255))
    else:
        pr.draw_rectangle_rounded_lines((tag_x, tag_y, tag_w, tag_h), 0.35, 8, RED)
        draw_text(font, "DEMO", tag_x + tag_w / 2, tag_y + tag_h / 2,
                  tag_h * 0.62, RED)

    draw_text(font, "ATTRACT MODE", tag_x + tag_w + W * 0.055, tag_y + tag_h / 2,
              tag_h * 0.50, DIM)

    # Panel progress bar: visible proof the screen is advancing on purpose.
    bar_w, bar_h_px = pw * 0.55, H * 0.007
    bar_x, bar_y = px + (pw - bar_w) / 2, py + ph + H * 0.022
    pr.draw_rectangle(int(bar_x), int(bar_y), int(bar_w), int(bar_h_px), (55, 62, 78, 255))
    pr.draw_rectangle(int(bar_x), int(bar_y), int(bar_w * (local / PANEL_SECONDS)),
                      int(bar_h_px), CYAN)

    draw_text(font, "HOW TO PLAY", W / 2, H * 0.20, H * 0.045, CYAN)

    # Progress pips, so it is obvious this is a loop of three and not a hang.
    pip_y = py + ph + H * 0.055
    for i in range(len(PANELS)):
        cx = W / 2 + (i - 1) * W * 0.028
        r = H * 0.009
        pr.draw_circle(int(cx), int(pip_y), r, CYAN if i == index else (90, 100, 115, 255))

    if int(t * 2) % 2 == 0:
        draw_text(font, "PRESS START TO PLAY", W / 2, H * 0.86, H * 0.05, AMBER)

    return True
