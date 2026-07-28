#!/usr/bin/env bash
# =============================================================================
# FLEX-OFF : one-command setup for a FRESH Raspberry Pi OS install
#
#   1. Copy the whole flexoff folder from the USB stick to the Pi
#   2. cd ~/flexoff
#   3. chmod +x SETUP.sh && ./SETUP.sh
#   4. sudo reboot
#
# Safe to re-run. Every step checks before it changes anything.
# =============================================================================

set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
say()  { echo -e "${GREEN}==>${NC} $*"; }
warn() { echo -e "${YELLOW} !${NC} $*"; }
die()  { echo -e "${RED}!!${NC} $*"; exit 1; }

GAME_DIR="$(cd "$(dirname "$0")" && pwd)"

echo
echo "======================================================"
echo "  FLEX-OFF cabinet setup"
echo "  Installing from: $GAME_DIR"
echo "======================================================"
echo

# --- 0. Sanity ---------------------------------------------------------------
[ -f "$GAME_DIR/main.py" ] || die "main.py not found. Run this from inside the flexoff folder."

if [ ! -d "$GAME_DIR/assets" ]; then
    warn "No assets/ folder found."
    warn "The game needs its GIFs, fonts, sounds and PNGs in $GAME_DIR/assets"
    warn "It will still start, but with placeholder graphics and no sound."
    echo
fi

# The game must live at ~/flexoff for the service file to find it.
if [ "$GAME_DIR" != "$HOME/flexoff" ]; then
    warn "Expected the game at $HOME/flexoff but it is at $GAME_DIR"
    read -r -p "    Copy it to $HOME/flexoff now? [Y/n] " ans
    if [[ ! "$ans" =~ ^[Nn]$ ]]; then
        mkdir -p "$HOME/flexoff"
        cp -r "$GAME_DIR/." "$HOME/flexoff/"
        say "Copied. Re-running from $HOME/flexoff"
        cd "$HOME/flexoff"
        chmod +x SETUP.sh
        exec "$HOME/flexoff/SETUP.sh"
    fi
fi

# --- 1. System packages ------------------------------------------------------
say "Updating package lists (this is the slow bit, ~1-2 min)"
sudo apt-get update -qq

say "Installing system dependencies"
sudo apt-get install -y -qq \
    python3-pip python3-venv \
    libgl1 libglu1-mesa libx11-6 libxcursor1 libxrandr2 libxinerama1 libxi6 \
    libasound2 libatomic1 \
    2>/dev/null || warn "Some optional packages were unavailable; continuing."

# --- 2. Python packages ------------------------------------------------------
say "Installing Python packages (raylib, pyserial, pillow)"
PIPFLAGS="--break-system-packages"
pip3 install $PIPFLAGS --upgrade pip -q 2>/dev/null || PIPFLAGS=""
pip3 install $PIPFLAGS -q raylib pyserial pillow \
    || die "pip install failed. Check the Pi has internet, then re-run."

python3 - <<'PY' || die "Python packages did not import. Setup cannot continue."
import importlib.util as u
missing = [m for m in ("pyray", "serial", "PIL") if u.find_spec(m) is None]
if missing:
    raise SystemExit("missing: " + ", ".join(missing))
print("   pyray, pyserial, pillow all import OK")
PY

# --- 3. Serial port permission ----------------------------------------------
# Without this the game cannot open /dev/ttyACM0 and silently falls back to
# simulation mode, which looks exactly like "the mainboard is broken".
if groups | grep -qw dialout; then
    say "Serial permission already granted (user is in 'dialout')"
else
    say "Adding $USER to the 'dialout' group for serial access"
    sudo usermod -aG dialout "$USER"
    NEED_REBOOT=1
    warn "This needs a reboot to take effect."
fi

# --- 4. Boot straight to the desktop, logged in ------------------------------
say "Enabling desktop autologin"
sudo raspi-config nonint do_boot_behaviour B4 2>/dev/null \
    || warn "Could not set autologin. Do it by hand: sudo raspi-config > System Options > Boot / Auto Login > Desktop Autologin"

# --- 5. Kiosk service --------------------------------------------------------
say "Installing the kiosk service"
chmod +x "$HOME/flexoff/kiosk/start-flexoff.sh"
mkdir -p "$HOME/.config/systemd/user"
cp "$HOME/flexoff/kiosk/flexoff.service" "$HOME/.config/systemd/user/flexoff.service"
systemctl --user daemon-reload
systemctl --user enable flexoff.service
sudo loginctl enable-linger "$USER" 2>/dev/null || true

# --- 6. Never blank the screen ----------------------------------------------
say "Disabling screen blanking and the screensaver"
# Wayland (Bookworm)
if [ -f "$HOME/.config/wayfire.ini" ] && ! grep -q "^\[idle\]" "$HOME/.config/wayfire.ini"; then
    printf '\n[idle]\ndpms_timeout = -1\nscreensaver_timeout = -1\n' >> "$HOME/.config/wayfire.ini"
fi
# X11 (Bullseye)
LXAUTO="$HOME/.config/lxsession/LXDE-pi/autostart"
if [ -d "$(dirname "$LXAUTO")" ] || mkdir -p "$(dirname "$LXAUTO")" 2>/dev/null; then
    grep -q "xset s off" "$LXAUTO" 2>/dev/null || {
        { echo "@xset s off"; echo "@xset -dpms"; echo "@xset s noblank"; } >> "$LXAUTO"
    }
fi

# --- 7. Hide the taskbar -----------------------------------------------------
PANEL="$HOME/.config/lxpanel/LXDE-pi/panels/panel"
[ -f "$PANEL" ] && sed -i 's/^\( *autohide=\).*/\1 1/' "$PANEL" 2>/dev/null || true

# --- 8. Quick self-test ------------------------------------------------------
say "Checking the game files import cleanly"
cd "$HOME/flexoff"
python3 - <<'PY' || warn "Import check failed - look at the error above."
import ast
for f in ("main.py", "UI.py", "juice.py", "attract.py", "AnimatedGIF.py"):
    ast.parse(open(f).read())
print("   all game files parse OK")
PY

echo
echo "======================================================"
say "Setup complete."
echo
if ls /dev/ttyACM* /dev/ttyUSB* >/dev/null 2>&1; then
    echo "   Mainboard detected: $(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | tr '\n' ' ')"
else
    warn "No mainboard on USB right now. The game will run in simulation mode."
fi
echo
echo "   Start now without rebooting :  systemctl --user start flexoff"
echo "   Watch the log               :  tail -f ~/flexoff/logs/flexoff.log"
echo "   Stop it (to edit code)      :  systemctl --user stop flexoff"
echo "   Quit from inside the game   :  Ctrl + Q"
echo
if [ "${NEED_REBOOT:-0}" = "1" ]; then
    warn "REBOOT REQUIRED for serial access:  sudo reboot"
else
    echo "   Reboot to confirm it comes up clean:  sudo reboot"
fi
echo "======================================================"
echo
