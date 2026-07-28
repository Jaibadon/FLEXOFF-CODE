"""
juice.py - the "game feel" layer.

Everything in here is purely cosmetic and purely additive: it draws on top of a
frame that is already correct. Nothing in this file can change the score, the
game state, or what the hardware reports, which is deliberate - it means a bug
in here can make the game ugly but never wrong, and it can be switched off
wholesale by not calling update()/draw().

Design notes
------------
* No screen shake during play. Shake is the first tool people reach for, but
  Flex-Off is a fine-motor tracking game: moving the target while someone is
  trying to hold a cursor on it actively fights the player. Shake is reserved
  for moments when nobody is aiming (the win screen).
* Particles are capped. An effects system that can spawn without limit is a
  frame-rate cliff waiting for the one judge who holds a perfect streak.
* Everything is sized off screen height, so it looks the same on the Pi's
  monitor as it does on a laptop.
"""

import math
import random
import pyray as pr


# Hard ceiling on live particles. On a Pi 4 the draw call cost is what matters,
# not the physics, so this is about protecting the frame budget.
MAX_PARTICLES = 220


def ease_out_back(t):
    """Overshoot-and-settle. Used for things that should feel like they land."""
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * pow(t - 1, 3) + c1 * pow(t - 1, 2)


def ease_out_cubic(t):
    return 1 - pow(1 - t, 3)


class _Popup:
    """A floating '+3' that rises, then fades."""
    __slots__ = ("x", "y", "text", "colour", "life", "max_life", "size", "vx")

    def __init__(self, x, y, text, colour, size, life=0.85):
        self.x, self.y = x, y
        self.text = text
        self.colour = colour
        self.life = self.max_life = life
        self.size = size
        # A little sideways drift so a rapid streak fans out instead of
        # stacking every number on the exact same pixel column.
        self.vx = random.uniform(-18, 18)

    def update(self, dt):
        self.life -= dt
        t = 1.0 - (self.life / self.max_life)
        self.y -= dt * 90.0 * (1.0 - t * 0.6)   # rises fast, then slows
        self.x += self.vx * dt
        return self.life > 0


class _Particle:
    __slots__ = ("x", "y", "vx", "vy", "life", "max_life", "colour", "size", "grav")

    def __init__(self, x, y, vx, vy, colour, size, life, grav=520.0):
        self.x, self.y = x, y
        self.vx, self.vy = vx, vy
        self.colour = colour
        self.size = size
        self.life = self.max_life = life
        self.grav = grav

    def update(self, dt):
        self.vy += self.grav * dt
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.life -= dt
        return self.life > 0


class Juice:
    def __init__(self, screen_h):
        self.h = screen_h
        self.popups = []
        self.particles = []
        self.flash_a = 0.0
        self.flash_col = (255, 255, 255)
        self.shake = 0.0
        self._confetti_timer = 0.0

    # ---------------------------------------------------------------- spawn
    def popup(self, x, y, text, colour):
        # Cap here too: a x3 streak at 30 Hz would otherwise queue 90 a second.
        if len(self.popups) < 40:
            self.popups.append(_Popup(x, y, text, colour, self.h * 0.035))

    def sparks(self, x, y, colour, n=8, speed=190.0):
        """A small burst, used every time a player lands on target."""
        budget = MAX_PARTICLES - len(self.particles)
        for _ in range(min(n, max(0, budget))):
            a = random.uniform(0, math.tau)
            s = random.uniform(0.35, 1.0) * speed
            self.particles.append(_Particle(
                x, y, math.cos(a) * s, math.sin(a) * s - 60,
                colour, random.uniform(2.0, 4.5) * (self.h / 720.0),
                random.uniform(0.28, 0.6)))

    def confetti(self, w, colours, n=90):
        """Win-screen celebration. Falls from above the top edge."""
        budget = MAX_PARTICLES - len(self.particles)
        for _ in range(min(n, max(0, budget))):
            self.particles.append(_Particle(
                random.uniform(0, w), random.uniform(-self.h * 0.3, 0),
                random.uniform(-40, 40), random.uniform(20, 140),
                random.choice(colours),
                random.uniform(3.0, 7.0) * (self.h / 720.0),
                random.uniform(1.6, 3.0), grav=90.0))

    def flash(self, colour, strength=0.55):
        """Full-screen tint that decays. Used for streak ignition and wins."""
        self.flash_col = colour
        self.flash_a = max(self.flash_a, strength)

    def kick(self, amount):
        """Screen shake. See the module note: NOT used during gameplay."""
        self.shake = max(self.shake, amount)

    def clear(self):
        self.popups.clear()
        self.particles.clear()
        self.flash_a = 0.0
        self.shake = 0.0

    # --------------------------------------------------------------- update
    def update(self, dt):
        # Guard against a stalled frame (window drag, USB hiccup) dumping a
        # huge dt in and teleporting every particle off-screen at once.
        dt = min(dt, 0.05)
        self.popups = [p for p in self.popups if p.update(dt)]
        self.particles = [p for p in self.particles if p.update(dt)]
        self.flash_a = max(0.0, self.flash_a - dt * 1.9)
        self.shake = max(0.0, self.shake - dt * 34.0)

    def shake_offset(self):
        if self.shake <= 0:
            return (0.0, 0.0)
        return (random.uniform(-self.shake, self.shake),
                random.uniform(-self.shake, self.shake))

    # ----------------------------------------------------------------- draw
    def draw_particles(self):
        for p in self.particles:
            t = p.life / p.max_life
            a = int(255 * min(1.0, t * 1.6))
            c = (p.colour[0], p.colour[1], p.colour[2], a)
            # Squares, not circles: cheaper, and it reads as pixel art.
            pr.draw_rectangle(int(p.x - p.size / 2), int(p.y - p.size / 2),
                              int(p.size), int(p.size), c)

    def draw_popups(self, font):
        for p in self.popups:
            t = p.life / p.max_life
            a = int(255 * min(1.0, t * 2.2))
            size = p.size * (0.75 + 0.25 * ease_out_back(min(1.0, (1 - t) * 4)))
            spacing = size / 10.0
            m = pr.measure_text_ex(font, p.text, float(size), spacing)
            x = p.x - m.x / 2
            y = p.y - m.y / 2
            # Cheap outline so numbers stay readable over a bright highway.
            pr.draw_text_ex(font, p.text, (x + 2, y + 2), float(size), spacing, (0, 0, 0, a))
            pr.draw_text_ex(font, p.text, (x, y), float(size), spacing,
                            (p.colour[0], p.colour[1], p.colour[2], a))

    def draw_flash(self, w, h):
        if self.flash_a > 0.004:
            pr.draw_rectangle(0, 0, int(w), int(h),
                              (self.flash_col[0], self.flash_col[1], self.flash_col[2],
                               int(self.flash_a * 255)))
