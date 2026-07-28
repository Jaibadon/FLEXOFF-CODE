#!/usr/bin/env bash
# =============================================================================
# Flex-Off kiosk launcher
#
# Wraps the game so the cabinet behaves like an arcade machine: it comes up on
# its own, it never shows a desktop, and if the game dies it comes straight
# back instead of dumping a judge onto a Raspberry Pi wallpaper.
# =============================================================================

set -u

GAME_DIR="${FLEXOFF_DIR:-$HOME/flexoff}"
LOG_DIR="$GAME_DIR/logs"
LOG="$LOG_DIR/flexoff.log"
PYTHON="${FLEXOFF_PYTHON:-python3}"

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# --- Keep the screen awake -------------------------------------------------
# A blanked screen 10 minutes into judging looks like a crashed project.
# Try every mechanism; whichever ones exist on this OS will take effect.
if [ -n "${DISPLAY:-}" ]; then
    xset s off        2>/dev/null || true
    xset -dpms        2>/dev/null || true
    xset s noblank    2>/dev/null || true
fi
# Wayland (Pi OS Bookworm) equivalent, if the compositor supports it
wlr-randr --output "$(wlr-randr 2>/dev/null | head -1 | cut -d' ' -f1)" --on 2>/dev/null || true

cd "$GAME_DIR" || { log "FATAL: game dir '$GAME_DIR' not found"; exit 1; }

# --- Wait for the mainboard (optional) -------------------------------------
# The game runs fine without it (it falls back to simulation mode), so this is
# a short courtesy wait rather than a hard requirement - never a reason to
# refuse to start in front of a judge.
for i in $(seq 1 10); do
    if ls /dev/ttyACM* /dev/ttyUSB* >/dev/null 2>&1; then
        log "Mainboard detected: $(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | tr '\n' ' ')"
        break
    fi
    [ "$i" -eq 1 ] && log "Waiting for mainboard USB..."
    sleep 1
done

# --- Supervised run loop ---------------------------------------------------
# systemd's Restart=always covers the service dying; this inner loop covers a
# Python-level crash and keeps the restart fast enough that a judge sees a
# flicker rather than a failure.
CRASHES=0
while true; do
    log "Starting Flex-Off (restart count: $CRASHES)"
    "$PYTHON" main.py >>"$LOG" 2>&1
    RC=$?

    # Exit code 0 means we quit on purpose (Ctrl+Q). Honour that and stop.
    if [ $RC -eq 0 ]; then
        log "Clean exit requested. Stopping."
        exit 0
    fi

    CRASHES=$((CRASHES + 1))
    log "Game exited with code $RC - restarting in 2s"

    # If it is crash-looping, something is genuinely broken. Back off so the
    # log stays readable instead of scrolling past at 100 restarts a second.
    if [ $CRASHES -ge 5 ]; then
        log "5+ consecutive crashes - backing off to 10s. Check the log above."
        sleep 10
    else
        sleep 2
    fi
done
