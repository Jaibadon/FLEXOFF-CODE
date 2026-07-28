# Flex-Off: kiosk boot + reliability pass

Everything here is drop-in. Nothing changes how the game plays.

---

## 1. Get it booting straight to the game

On the Pi:

```bash
# Put the project at ~/flexoff  (main.py, UI.py, AnimatedGIF.py, assets/, kiosk/)
cd ~/flexoff/kiosk
chmod +x *.sh
./install-kiosk.sh
sudo reboot
```

That is the whole install. The Pi will now power on straight into Flex-Off with no desktop, no cursor, and no screen blanking.

**Controls once it is running**

| Action | Key |
|---|---|
| Quit deliberately | `Ctrl` + `Q` |
| Stop the service (to edit code) | `systemctl --user stop flexoff` |
| Start it again | `systemctl --user start flexoff` |
| Watch the log live | `tail -f ~/flexoff/logs/flexoff.log` |
| Turn kiosk mode off for good | `systemctl --user disable --now flexoff` |

A lone `ESC` no longer quits. On a cabinet that is far too easy to hit by accident, and a judge who exits to a Raspberry Pi wallpaper has just watched your project "crash".

### What the installer does

1. Installs any missing Python packages.
2. Adds you to the `dialout` group. **Without this the game cannot open `/dev/ttyACM0` and silently falls back to simulation mode**, which looks identical to a dead board. Worth knowing if you have ever been confused by that.
3. Sets desktop autologin.
4. Installs a systemd **user** service with `Restart=always`.
5. Kills screen blanking on both Wayland (Bookworm) and X11 (Bullseye).
6. Auto-hides the taskbar.

### Two layers of crash recovery

The launcher script re-runs the game if Python dies, and systemd re-runs the launcher if the whole thing dies. A crash becomes a two-second flicker instead of a dead demo. After five consecutive crashes it backs off to ten seconds so the log stays readable rather than scrolling past at machine speed.

### Optional: remove the boot text

Ten seconds of Linux boot messages breaks the arcade illusion. In `/boot/firmware/cmdline.txt` (one single line, append to the end):

```
quiet splash logo.nologo vt.global_cursor_default=0
```

Put a 1920x1080 PNG of your title art at `/usr/share/plymouth/themes/pix/splash.png` and the Pi boots black to your artwork to the game.


---

## 2. Controls: two unlabelled buttons

The cabinet has exactly two red buttons, one left and one right, and nothing written on them. Rather than printing labels on the panel (which would be wrong on half the screens), **the display shows what each button does right now**, drawn at the bottom-left and bottom-right so the on-screen position matches where your hands are.

| Screen | LEFT button | RIGHT button |
|---|---|---|
| Title | START | START |
| Mode select | CHANGE MODE | CONFIRM |
| Calibration (PvP) | CALIBRATE P1 | CALIBRATE P2 |
| Calibration (1P) | CALIBRATE ME | CALIBRATE ME |
| Countdown / Playing | *(idle)* | *(idle)* |
| Game over | PLAY AGAIN (or NEXT LEVEL) | MAIN MENU |

The legend sits along the **top** of the screen, matching where the buttons physically are on the cabinet.

**PLAY AGAIN keeps your calibration.** It drops you straight back into the same mode at the same difficulty with both players' ranges intact, because nobody wants to re-run a 6-second relax/flex just to have another go. The one exception is the CPU ladder: beat a bot and the left button reads **NEXT LEVEL** and promotes you. **MAIN MENU** is a full reset to the title.

Two principles behind this. **Left button always means the left-hand player** - the one mapping nobody has to be told. And during a round both buttons do nothing at all, so the legend disappears rather than promising something that will not happen; the only input while playing is a double-tap on your own armband.

Double-tap also works as a confirm on every menu screen, so a player who has already strapped in never has to reach for the panel.

### The third button is gone

The old build needed three (`START`, `CAL1`, `CAL2`). Mode select now uses LEFT-to-cycle and RIGHT-to-confirm, which is the standard two-button arcade pattern.

The interesting one was calibration, which used to end with "now press START". That was the single biggest stall point for a first-time player, and with no third button it had to go. **Calibration now hands over to the countdown by itself** once everyone required has been ready for **10 seconds**, with the remaining seconds and a drain bar shown on screen so the wait never reads as a hang.

10 seconds is deliberately generous: long enough to reposition an electrode, read the screen, or let a second player finish.

### What double-tap does, precisely

On the calibration screen, a double-tap means **calibrate (or re-calibrate) me** - it never starts the game. The trigger is:

```cpp
start_p1 = (btn_cal1_pressed || p1_tap) && (p1_calib_state != 1) && p1_is_human;
```

So tapping while you are *not* mid-calibration re-runs your own calibration, which also resets the 10-second clock. That is the deliberate way back if you are unhappy with your range. A tap is ignored while a calibration is already running, so a stray knock cannot interrupt one in progress.

On the title, mode-select and game-over screens a double-tap acts as confirm, so a player who has already strapped in never has to reach for the panel.

`BTN_LEFT` and `BTN_RIGHT` are pins 6 and 7 at the top of the firmware. Pin 5 is still read as an optional third button if you ever fit one, but nothing depends on it.

---

## 3. Leads-off alert

The AD8232's `leads_off` bit was **already arriving** in every telemetry packet and was being thrown away. It is now plumbed all the way through to the screen.

A compact warning appears in that player's half of the screen: left half for P1, right half for P2. It pulses so it catches peripheral vision while someone is staring at the highway, but it does not blank the screen, because a fault is not a reason to stop showing the game.

Two states, because they need different fixes from the player:

| Shown | Means | Cause |
|---|---|---|
| `P1 LEADS OFF` / CHECK ELECTRODES | Electrode has lifted off skin | Pad peeled, dried gel, bad prep |
| `P1 NO SIGNAL` / CHECK ARMBAND POWER | The mainboard has not heard that armband for 350 ms | Flat battery, out of range, brownout |

`NO SIGNAL` deliberately outranks `LEADS OFF`: if the board cannot hear the armband at all, what its electrodes were doing a second ago is not useful information.

It is suppressed where it would be nonsense: on the title and mode-select screens (nobody has strapped in yet), and for the P2 slot in solo or vs-CPU modes (the bot has no electrodes). In solo mode when the human claimed armband 2, the warning follows them to the left-hand slot where they are drawn.

If the Pi loses the mainboard entirely, every armband shows `NO SIGNAL` rather than a confident and wrong "all fine".


---

## 4. Game feel and UX additions

All of this is **additive**: it draws on top of a frame that is already correct. Nothing in the effects layer can change the score, the game state, or what the hardware reports, so a bug in it can make the game ugly but never wrong.

### Attract mode (`attract.py`)

After six idle seconds on the title screen, the cabinet starts teaching itself.

**It is labelled as a demo so it can never read as a fault.** A screen that animates on its own looks like a hang or a glitch if nothing says otherwise, so there are three cues: a blinking **DEMO** tag in the top corner (the arcade convention), the words ATTRACT MODE beside it, and a live progress bar under the panel showing it is counting toward the next one rather than stuck. The standing PRESS START prompt also proves input is still being accepted. Three animated panels cycle on a loop:

1. **FLEX TO CLIMB** - an arm that pumps, with electrode pads drawn on the bicep (which silently answers "where do these stick?"), next to a gauge that fills as it flexes.
2. **STAY ON THE LINE** - a scrolling target wave with a cursor tracking it, turning green and popping "+1" when it lands.
3. **WIN THE TUG-OF-WAR** - the flag being dragged toward a win line.

This is the single highest-value addition for judging. A judge walks up knowing nothing, and every second they spend working out what the machine wants is a second of a short slot burned. Confusion reads as a design flaw even when the hardware is perfect. Real cabinets solved this decades ago; now yours does too.

It is drawn entirely procedurally, with no image files, so it cannot fail with a missing-asset error on the day.

### Live signal bar during calibration

This closes the worst UX gap in the whole flow. During the six-second relax/flex calibration the player previously got **no feedback whatsoever** that the machine could see them, because position cannot be mapped until min/max are known. Six silent seconds is exactly where someone concludes the thing is broken.

There is now a live bar fed by the raw envelope, which auto-ranges to whoever is wearing it (it tracks the peak seen this calibration, decaying slowly so one noise spike does not flatten it). It runs green when relaxed and warms to red as they push, so the colour alone tells them whether they are doing the current phase right.

### Effects (`juice.py`)

- **Score popups** - floating "+N" from the cursor, driven off the actual score delta so they can never disagree with the board. They fan out sideways slightly so a fast streak does not stack every number on one pixel column.
- **On-target halo** - a soft glow around the cursor while scoring. This matters more than the number, because a player's eyes are locked on the cursor and never on the scoreboard.
- **Sparks** - a small burst on target, rate-limited rather than per-frame.
- **Streak ignition flash** - a full-screen tint the moment the multiplier lights up. Edge-triggered, so it fires once per ignition rather than every frame it is held.
- **Win celebration** - confetti and a screen kick on a win, a red flash on a loss. Fires exactly once on entry.
- **Countdown polish** - numbers punch in with an ease-out-back overshoot and an expanding shockwave ring, ending on **GO!**

**Deliberately no screen shake during play.** Shake is the first tool everyone reaches for, but Flex-Off is a fine-motor tracking game: moving the frame while someone is trying to hold a cursor on a line actively fights the player. Shake is used in exactly one place, the win screen, where nobody is aiming at anything.

**Particles are hard-capped at 220.** An effects system that can spawn without limit is a frame-rate cliff waiting for the one judge who holds a perfect streak. Verified: 2000 spawn attempts in a single frame still lands on the cap, and a stalled frame (huge `dt`) is clamped so nothing teleports off screen.




---

## 5. High score board and the name spinner

**Only Solo Timed Rush feeds the leaderboard.** That is the score-attack mode; a tug-of-war score is a margin over an opponent, not a personal best, so putting those on the same board would be comparing different things.

### Spinning a name

Classic cabinets let you spell three letters with a joystick. This one has two buttons and no stick, and a judging slot is short. So instead:

- Finish a solo run scoring **20 or more**
- The board rolls you a name: **CRIMSON WYVERN**, **AZURE KRAKEN**, **EMERALD MANTICORE**
- **LEFT = RESPIN**, **RIGHT = KEEP IT**
- Then the leaderboard appears with your entry pulsing so you can find yourself

24 adjectives x 24 beasts gives 576 combinations. It reads instantly, it is one button press instead of nine, people enjoy the result far more than "AAA", and it sidesteps the obvious problem with free text entry on a public machine.

The mainboard only rolls two **indices**. The word lists live in `highscores.py`, so there is exactly one place to edit them and the board stays tiny.

### Where you see it

- **On the mode carousel** as a fourth option, `HIGH SCORES` (read-only, any button leaves)
- **In the attract loop** as a fourth panel showing the top five, so the cabinet advertises its own records while idle

### Storage

A plain `highscores.json` next to the game, top 8. Written via a temp file and an atomic rename, because a power cut mid-write on an arcade cabinet is exactly the moment it would happen and a half-written file that fails to parse would take the board out permanently. If the file is missing or corrupt the board simply starts empty rather than crashing the game.

---

## 6. Using the accelerometer: motion artifact detection

The LIS3DH was sending XYZ at 100 Hz and the game was throwing all of it away. It now does the one thing an accelerometer is genuinely good for on this project.

**Motion artifact is the classic contaminant of surface EMG.** Shaking, swinging or knocking the arm drags the electrode against the skin and injects a large low-frequency transient straight into the measurement band. The 17 Hz high-pass in the analog front end exists precisely to suppress this, but no filter removes it completely, and a hard enough shake still reads as effort.

The accelerometer measures that disturbance **directly**, which means the game can tell a player their signal is being corrupted. It also catches the obvious way to cheat, which is to shake your arm instead of contracting the muscle.

### How it works

The mainboard tracks a slow baseline of the acceleration magnitude and measures how far the instantaneous value departs from it:

```cpp
mag    = sqrt(ax^2 + ay^2 + az^2)
base  += (mag - base) * 0.02      // slow: tracks posture and orientation
motion += (|mag - base| - motion) * 0.25   // fast: the shake itself
```

Comparing against a **learned** baseline rather than a fixed 1 g is what makes it work regardless of how the band happens to be worn. With your resting reading of `(-137, 11, -62)` the magnitude is 151 and motion settles to 0; a hard shake pushes the reported level to about 72, well over the 60 threshold where the hint appears.

### What the player sees

A **HOLD STILL / shaking is not flexing** hint in that player's half, fading in between 60 and 100 rather than popping on. It is suppressed when the electrodes are already flagged, because leads-off is the more urgent problem and two stacked warnings is just noise.

**Advisory only, deliberately.** It does not block scoring. A false positive that silently stopped a judge from scoring would be far worse than a hint they can ignore. If you want it to actually gate scoring, the level is already transmitted and the change is a couple of lines.

### Why it is worth mentioning to a judge

It closes the loop on the filter design story. You can say: "motion artifact is what the 17 Hz high-pass is for, no filter removes it entirely, so we measure it directly with the accelerometer and tell the player" — and then demonstrate it in five seconds by shaking your arm. That connects the analog design, the sensor choice and the UX in one sentence.

**No armband reflash needed.** The XYZ was already in the telemetry packet; this is all computed on the mainboard.

---

## 7. Power Surge (the double-tap payoff)

The LIS3DH double-tap was already detected, already plumbed through ESP-NOW, and completely **unused during a round** - it only did anything on the menu and calibration screens. So the hardware for this was already sitting there doing nothing.

### How it plays

1. Build a streak until you **catch fire** (x2 or better).
2. A pulsing **DOUBLE TAP!** prompt appears under your multiplier badge.
3. Whack your armband twice and you bank **15 x your current multiplier**, instantly.
4. **One per streak.** To earn another you have to lose the streak and rebuild it.

**You get a 2-second grace window after firing.** Reaching over to whack your own arm costs you the tracking you spent several seconds earning, so without this the reward for a perfect streak was to immediately lose it. During the window you count as on-target even off the line, shown as a ring around your cursor that shrinks and shifts violet to amber to red as it drains, so it never looks like the hit detection has simply broken.

The decision that makes it interesting: fire it now at x2 for +30, or hold out for x3 and +45 and risk losing the whole streak before you get there. It rewards the player who is already doing well without ever handing them the match.

### Balance

A match is won on a 200-point lead, and perfect play scores 30/sec at x1.

| Fired at | Bonus | Share of the winning gap |
|---|---|---|
| x2 | +30 | 15% |
| x3 | +45 | 22% |

Enough to feel decisive in the moment, never enough to actually decide the match. If you want it swingier, `SURGE_BONUS` is a single constant at the top of the firmware.

### Turning it off

One line, at the top of `firmware/main.cpp`:

```cpp
const bool SURGE_ENABLED = true;   // <-- false disables the whole feature
```

Set it to `false` and the game behaves exactly as it did before: no bonus, no prompt, no celebration, and double-tap goes inert during a round. **Nothing on the Pi needs changing to match**, because the surge field just reports 0 forever and the display has nothing to draw. That also means a board flashed with it off works fine against a Pi that supports it, and vice versa.

### Why it is worth demoing

It is the one moment where the accelerometer visibly does something a judge can feel. The EMG front end is the hard engineering, but it is invisible - a judge sees a bar move. Whacking your own arm and watching the screen erupt is the part they will remember, and it costs you one sentence to explain.

---

## 8. Bugs fixed

**The serial thread was pegging a CPU core.** `while True: if ser.in_waiting > 0` with no sleep spins as fast as the CPU allows. On a four-core Pi that is 25% of your total compute burned on doing nothing, stolen directly from the renderer. It now blocks in `readline()` with a timeout, which costs nothing while idle. Expect noticeably steadier frame pacing.

**The serial thread died permanently on any disconnect.** `ser.in_waiting` sat outside the `try`, so unplugging the mainboard, or it browning out on the 2.8 V rail, raised an exception that killed the thread. The game then ran forever on stale values with no indication anything was wrong. It now detects the drop, closes the handle, and reconnects when the board returns. It also auto-detects the port, so it survives the board coming up as `ttyACM1`.

**`xQueueSendFromISR` was called from a task, not an ISR.** The ESP-NOW receive callback runs in the WiFi task. Calling the `FromISR` variant there is undefined behaviour that happens to work most of the time. Now `xQueueSend(..., 0)`.

**Second solo run showed `TIME: 0`.** `play_start` was set on entry to PLAYING and never cleared, so the next solo game read a timestamp from the previous one and started expired. Now reset whenever the state is not PLAYING.

**The solo timer was running on two clocks.** `UI.py` kept its own `time.time()` stopwatch against a hard-coded `TOTAL_TIME = 60`, while the board ends the round at tick 1800. Those disagreed by however long the Pi took to notice the state change, and drifted further if the loop ran off 30 Hz. The board now computes the remaining seconds and transmits them, and the Pi just displays the number. There is exactly one clock, and `SOLO_TICK_LIMIT` in the firmware is the only place the length is defined. If the board is running older firmware the Pi derives it from the transmitted tick count instead - still the board's clock, never a local one.

**"GO!" never appeared.** I added a 3-2-1-**GO!** countdown, but the board switched to PLAYING at exactly 3000 ms, which is the same instant the "1" finishes. The GO beat was drawn for at most one frame and usually not at all. The board now holds the countdown for 4000 ms so GO gets a full second of its own. A good example of a bug that only exists in the seam between two components that were each individually correct.

**Stale on-screen instructions.** Going to two buttons left a trail of text telling players to "Press START", "Press CAL 1/2 to scroll", and "Press Mainboard Calibration Buttons" - all referring to hardware that no longer exists. Seven separate strings across the title, mode-select, calibration and game-over screens. All replaced with either the button legend or wording that matches the real panel.

**Calibration now matches the game exactly, and I verified it rather than assuming.**

The mapping is the same code path in both states:

```cpp
// STATE_CALIBRATION (live gauge)        STATE_PLAYING (highway)
p1_pos = map(p1_val, p1_min, p1_max, 0, 100);   // identical
p1_pos = constrain(p1_pos, 0, 100);             // identical
```

Same filtered signal (`p1_val`), same `p1_min`/`p1_max`, same output range, same clamp. The armband-2 swap for solo players is applied identically in both. On the display side both screens use `y = top + span * (1 - pos/100)`, so 100 is at the top and 0 at the bottom in each; only the physical span differs, which is size, not calibration.

I also added a **practice target** to the calibration gauge. It moves exactly like the real one at level 0 and uses the same `HIT_TOLERANCE = 12`, turning your cursor green when you are on it. Since the effort-to-position mapping was already identical, adding the target means the calibration screen is now a genuine preview: you can confirm you can reach both ends of the range and get a feel for the tracking before committing. There is also a faint band showing where the target actually roams (it is clamped to 10-90), so you can see you never need the absolute extremes.

**What was wrong before.** This is the one that actually spoils a round. The calibration routine ended with a hidden line:

```cpp
p1_max = p1_min + (int)((p1_max - p1_min) * 0.75f);
```

You flexed to your true maximum during calibration, and the game then stored only **75%** of that range. The top of the highway was reachable at three-quarters of the effort you had just demonstrated, so a range that felt right during setup behaved differently the moment play started. It is now a named constant, `CALIB_HEADROOM`, defaulting to **1.0**: what you calibrate is exactly what the game expects. Lower it if players find holding the top too tiring, but it reintroduces the mismatch.

(Worth noting what was *not* wrong: `p1_val` is just the rounded `p1_env_filt`, so the relax and flex phases were measuring the same signal. Only the headroom scaling was at fault.)

**Buttons could double-register.** There was no debounce anywhere, just edge detection on a raw `digitalRead`. At 30 Hz most contact bounce is missed, but a press landing on a sample boundary could register twice and skip two game modes in one press. There is now a 40 ms lockout after each accepted edge, plus a `BUTTON_DEBUG` flag that prints the raw state of all three pins once a second - which is the fastest way to tell whether a dead button is a wiring problem or a logic one.

**A variable shadowing bug I introduced and then caught.** The calibration screen already uses the names `p1_status`/`p2_status` for its `"READY"`/`"WAITING"` label strings. My health values collided with them, which made the alert render with a string where an int belonged. Renamed to `p1_health`/`p2_health`. Mentioning it because it is exactly the kind of thing that would have looked fine until a judge opened the calibration screen.

**Music could crash on a corrupt packet.** `target_music` was assigned by an if/elif chain covering states 0 to 5 with no `else`. A garbled packet carrying state 6 left it unbound and raised `NameError` on the next line. Now defaults.

**The window was upscaling.** `init_window(1280,720)` then `toggle_fullscreen()` renders 720p and lets the compositor stretch it to 1080p, softening pixel art for no performance gain. It now asks the monitor its resolution and matches it 1:1.

---

## 9. Protocol change

The serial line went from 14 to 28 fields. Fourteen appended:

```
<...,p1_status,p2_status,p1_env,p2_env,p1_surge,p2_surge>
        |         |        |      |       |        |
   0 OK / 1 LEADS OFF   raw envelope,   0 none / 1 armed / 2 just fired
   / 2 NO LINK          calibration bar
```

Field 21 is `time_left` (authoritative seconds remaining in Timed Solo Rush, -1 elsewhere) and field 22 is `ready_cd` (the calibration auto-start countdown, -1 when not counting). Fields 23/24 are the accelerometer motion level (0-100), 25/26 the surge grace window (0-100), and 27/28 the two name-spinner indices. The Python parser accepts any of 10, 12, 14, 16, 18, 20, 21, 22 or 24 fields, so **an old ESP32 build still works with the new Pi code** - the newer features just stay quiet. Flash order does not matter. Tested against malformed, truncated and mid-stream-garbage lines; none of them can take the reader down.

---

## 10. Modes and difficulty, audited

There are **three** game modes and none of them are leftovers:

| Mode | What it is |
|---|---|
| 1 | 1 PLAYER vs CPU, tug-of-war against the AI |
| 2 | 2 PLAYER PvP, head-to-head |
| 3 | SOLO TIMED RUSH, 60 s score attack (the one that feeds the leaderboard) |

Mode 4 on the carousel is the read-only high score board, not a game.

Two separate difficulty systems, and both are sound:

**`difficulty` (1-3)** applies only to vs CPU and controls how accurate the bot is: `max_error = 40 / difficulty`, so easy wanders +/-40 and hard only +/-13. Beating a bot promotes you a tier.

**`level` (0-2)** applies to every mode and ramps automatically every 20 seconds, making the target harder to follow: level 0 is a plain sine, level 1 adds a slow drift, level 2 adds a fast wobble on top.

Nothing needed removing.

---

## 11. What I deliberately did not touch

It is the night before. These are all real improvements and all of them carry risk, so they are listed rather than done:

- **Textures are never unloaded** in `AnimatedGIF`. Only leaks at startup, so it does not matter for a demo.
- **No attract mode.** A real cabinet loops a demo when idle. Genuinely nice, genuinely not worth writing tonight.

---

## 12. Fifteen-minute test before you sleep

1. `sudo reboot`, hands off, confirm it lands in the game with no desktop flash.
2. Peel one electrode off mid-round. The alert should appear in the correct half within about a second, and clear when you reattach.
3. Unplug the mainboard USB mid-game. Every armband should show `NO SIGNAL`. Plug it back in. It should reconnect on its own without touching the Pi.
4. Play two solo rounds back to back and confirm the second one starts at 60 seconds, not 0.
5. `systemctl --user stop flexoff` then `start`, to be sure you can get out and back in in front of a judge.
6. Sit on the title screen for ten seconds and confirm the how-to-play demo starts and cycles all three panels.
7. Start a calibration and confirm the live signal bar moves when you flex. This is the one a judge is most likely to try themselves.
8. Build a streak to x2, confirm the DOUBLE TAP prompt appears, whack the armband, and check the score jumps by 30. Then confirm a second tap on the same streak does nothing.
9. Walk the menus using only the two buttons and check the on-screen legend always matches what they actually do.
10. Finish a calibration and confirm the countdown appears, ticks 10 down to 0, and the game starts by itself. Double-tap partway through and check it re-calibrates and resets the clock.
11. Watch the countdown and confirm you actually see **GO!** before play starts.

### Testing without the board

Simulation mode runs if no mainboard is found, and now covers the new features:

| Key | Effect |
|---|---|
| `0`-`5` | jump to a game state |
| `W`/`S`, `Up`/`Down` | move P1 / P2 |
| `C`/`V` | cycle P1 / P2 calibration phase |
| `T`/`Y` | toggle P1 / P2 leads-off warning |
| `B` | toggle P1 "no link" |
| `G`/`H` | fire P1 / P2 power surge |
| `M` | toggle the HOLD STILL motion warning |
| `6` / `7` | jump to name entry / high score board |
| `Ctrl`+`Q` | quit |
