#!/usr/bin/env bash
# =============================================================================
# Flex-Off kiosk installer  -  run ONCE on the Raspberry Pi
#
#   cd ~/flexoff/kiosk && chmod +x *.sh && ./install-kiosk.sh
#
# Afterwards the Pi boots straight into the game. Nothing else to click.
# =============================================================================

set -e

GAME_DIR="$HOME/flexoff"
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
say() { echo -e "${GREEN}==>${NC} $*"; }
warn() { echo -e "${YELLOW}!!${NC} $*"; }

say "Installing Flex-Off kiosk mode"

# --- 0. Sanity check -------------------------------------------------------
if [ ! -f "$GAME_DIR/main.py" ]; then
    warn "Expected the game at $GAME_DIR/main.py but it is not there."
    warn "Put the project in ~/flexoff (or edit GAME_DIR at the top of this script)."
    exit 1
fi

chmod +x "$GAME_DIR/kiosk/start-flexoff.sh"

# --- 1. Python dependencies ------------------------------------------------
say "Checking Python packages"
python3 - <<'PY' || pip3 install --break-system-packages raylib pyserial pillow
import importlib, sys
missing = [m for m in ("pyray", "serial", "PIL") if importlib.util.find_spec(m) is None]
sys.exit(1 if missing else 0)
PY

# --- 2. Serial port permission --------------------------------------------
# Without this the game cannot open /dev/ttyACM0 and silently falls back to
# simulation mode, which looks exactly like "the board is broken".
if ! groups | grep -qw dialout; then
    say "Adding $USER to the 'dialout' group (needed to read the mainboard)"
    sudo usermod -aG dialout "$USER"
    warn "You must REBOOT for this group change to take effect."
fi

# --- 3. Autologin to the desktop ------------------------------------------
say "Enabling desktop autologin"
sudo raspi-config nonint do_boot_behaviour B4 2>/dev/null \
    || warn "Could not set autologin automatically. Do it manually: sudo raspi-config > System Options > Boot / Auto Login > Desktop Autologin"

# --- 4. Install the user service ------------------------------------------
say "Installing systemd user service"
mkdir -p "$HOME/.config/systemd/user"
cp "$GAME_DIR/kiosk/flexoff.service" "$HOME/.config/systemd/user/flexoff.service"
systemctl --user daemon-reload
systemctl --user enable flexoff.service

# Let the service keep running without an active login session.
sudo loginctl enable-linger "$USER" 2>/dev/null || true

# --- 5. Stop the screen blanking -----------------------------------------
say "Disabling screen blanking and the screensaver"
mkdir -p "$HOME/.config/wayfire"   # Bookworm / Wayland
if [ -f "$HOME/.config/wayfire.ini" ] && ! grep -q "idle" "$HOME/.config/wayfire.ini"; then
    printf '\n[idle]\ndpms_timeout = -1\nscreensaver_timeout = -1\n' >> "$HOME/.config/wayfire.ini"
fi
# Bullseye / X11
mkdir -p "$HOME/.config/lxsession/LXDE-pi"
AUTOSTART="$HOME/.config/lxsession/LXDE-pi/autostart"
if [ -f "$AUTOSTART" ] || [ -d "$(dirname "$AUTOSTART")" ]; then
    grep -q "xset s off" "$AUTOSTART" 2>/dev/null || {
        echo "@xset s off"     >> "$AUTOSTART"
        echo "@xset -dpms"     >> "$AUTOSTART"
        echo "@xset s noblank" >> "$AUTOSTART"
    }
fi

# --- 6. Hide desktop clutter ---------------------------------------------
say "Hiding the taskbar so no desktop shows behind the game"
PANEL="$HOME/.config/lxpanel/LXDE-pi/panels/panel"
[ -f "$PANEL" ] && sed -i 's/^\( *autohide=\).*/\1 1/' "$PANEL" 2>/dev/null || true

echo
say "Done."
echo
echo "  Start now without rebooting :  systemctl --user start flexoff"
echo "  Watch the log               :  tail -f ~/flexoff/logs/flexoff.log"
echo "  Stop it (e.g. to edit code) :  systemctl --user stop flexoff"
echo "  Disable kiosk permanently   :  systemctl --user disable --now flexoff"
echo
echo "  In the game, Ctrl+Q quits deliberately (a lone ESC will not)."
echo
warn "REBOOT now to confirm it comes up clean:  sudo reboot"
