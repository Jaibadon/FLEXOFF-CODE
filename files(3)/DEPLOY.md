# FLEX-OFF : what to put on the USB stick

Assumes a Raspberry Pi with **nothing on it but a fresh Raspberry Pi OS (Desktop)**.

---

## The folder to copy

Put this whole folder on the USB stick as `flexoff`:

```
flexoff/
├── SETUP.sh                    <- run this once on the Pi
├── main.py                     <- game entry point
├── UI.py                       <- all screen drawing
├── juice.py                    <- particle / popup effects
├── attract.py                  <- idle "how to play" demo
├── AnimatedGIF.py              <- GIF playback helper
├── README.md                   <- what changed and why
├── DEPLOY.md                   <- this file
│
├── assets/                     <- YOUR ART AND SOUND (see below)
│
├── kiosk/
│   ├── flexoff.service         <- systemd unit, restarts on crash
│   ├── start-flexoff.sh        <- launcher with logging + retry
│   ├── install-kiosk.sh        <- kiosk-only installer (SETUP.sh calls this work itself)
│   └── flexoff-autostart.desktop
│
└── firmware/
    └── main.cpp                <- ESP32-S3 mainboard. Flash from your laptop,
                                   NOT from the Pi. Included so the whole
                                   project travels together.
```

## assets/ is not optional

The game loads these by name. **Copy your existing `assets/` folder across** — none of it is in this package.

| File | Used for |
|---|---|
| `font.ttf` | all text (falls back to a blocky default) |
| `waterfall temple background.gif` | animated background |
| `player 1.gif`, `player 2.gif` | the two characters |
| `easy_bot.gif`, `medium_bot.gif`, `hard_bot.gif` | CPU opponents |
| `tug of war.gif` | rope centrepiece |
| `flag.png`, `rope.png`, `solo_target.png`, `scoring_effect.png` | tug-of-war pieces |
| `pixil-layer-Background.png` | menu backdrop |
| `theme_song.mp3`, `menu_song.mp3` | music |
| `start_btn.mp3`, `cal_btn.mp3`, `countdown.mp3` | sound effects |
| `on_fire.mp3` | optional streak voice line |

Missing files degrade gracefully (placeholder or silence) rather than crashing, but the cabinet will look unfinished.

---

## On the Pi

```bash
# 1. Plug the USB stick in, then:
cp -r /media/$USER/*/flexoff ~/flexoff

# 2. Run setup (needs internet for pip)
cd ~/flexoff
chmod +x SETUP.sh
./SETUP.sh

# 3. Reboot
sudo reboot
```

The Pi now boots straight into the game. No desktop, no cursor, no screen blanking.

**The Pi needs internet for step 2** to install `raylib`, `pyserial` and `pillow`. Do this before the day if you can — a competition venue's wifi is not something to depend on.

---

## Checks after reboot

| Check | Expect |
|---|---|
| Pi powers on | lands in the game, no desktop flash |
| `tail -f ~/flexoff/logs/flexoff.log` | "Connected to mainboard on /dev/ttyACM0" |
| Unplug mainboard mid-game | armbands show NO SIGNAL, reconnects on replug |
| `Ctrl` + `Q` | quits deliberately |

If it says **"No mainboard found - running in SIMULATION mode"** while the board *is* plugged in, the `dialout` group change has not taken effect. Reboot once more.

---

## Flashing the mainboard

`firmware/main.cpp` goes on the **ESP32-S3 mainboard**, flashed from your laptop with PlatformIO or the Arduino IDE. It is not built on the Pi.

The two armband nodes run separate firmware and do **not** need reflashing for any of this — every new feature is computed on the mainboard from data the nodes were already sending.

Check before flashing:

```cpp
#define BTN_LEFT  6      // your left red button
#define BTN_RIGHT 7      // your right red button
const bool SURGE_ENABLED = true;    // false disables the double-tap power-up
```

---

## If the game will not start

```bash
systemctl --user status flexoff      # is the service running?
tail -50 ~/flexoff/logs/flexoff.log  # what did it say on the way down?
systemctl --user stop flexoff        # stop the kiosk
cd ~/flexoff && python3 main.py      # run by hand to see the error
```
