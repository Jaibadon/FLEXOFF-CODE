"""
highscores.py - the leaderboard and its random name generator.

Why random names instead of letter entry
----------------------------------------
Classic arcade cabinets let you spell three letters with a joystick. This
cabinet has two buttons and no stick, and a judging slot is short. Spinning a
name is one button press, reads instantly, and produces something people
actually enjoy ("CRIMSON WYVERN" beats "AAA"). It also sidesteps the obvious
problem with free text entry on a public machine.

Storage is a plain JSON file next to the game. If it is missing or corrupt the
board simply starts empty rather than taking the game down with it - on a
competition machine, losing the leaderboard is survivable, failing to boot is
not.
"""

import json
import os
import random

SCORES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "highscores.json")
MAX_ENTRIES = 8

# --- Name parts -------------------------------------------------------------
# Kept to short, punchy, screen-safe words. Lengths are deliberately similar so
# a spun name never overflows the panel it is drawn in.
ADJECTIVES = [
    "CRIMSON", "AZURE", "GOLDEN", "EMERALD", "VIOLET", "SCARLET",
    "OBSIDIAN", "SILVER", "COBALT", "AMBER", "JADE", "IRON",
    "SAVAGE", "TURBO", "ATOMIC", "COSMIC", "FERAL", "MIGHTY",
    "RAPID", "THUNDER", "NEON", "SOLAR", "FROZEN", "BLAZING",
]

BEASTS = [
    "WYVERN", "GRIFFIN", "KRAKEN", "PHOENIX", "HYDRA", "BASILISK",
    "CHIMERA", "MANTICORE", "DRAGON", "SPHINX", "CERBERUS", "MINOTAUR",
    "FALCON", "PANTHER", "RHINO", "VIPER", "MAMMOTH", "JAGUAR",
    "BADGER", "STALLION", "GRIZZLY", "OSPREY", "COBRA", "LYNX",
]


def name_from_seed(a, b):
    """
    Turn the two indices the mainboard sends into a name.

    Using indices rather than sending the text means the board stays tiny and
    the word lists live in one place. Modulo keeps it safe if the board ever
    sends something out of range.
    """
    return "%s %s" % (ADJECTIVES[a % len(ADJECTIVES)], BEASTS[b % len(BEASTS)])


def random_seed():
    """A fresh pair of indices, for local use when simulating."""
    return random.randrange(len(ADJECTIVES)), random.randrange(len(BEASTS))


# --- Persistence ------------------------------------------------------------
def load():
    """
    Read the table. Returns a list of {"name", "score", "mode"} sorted high to
    low. Any failure gives an empty board rather than an exception.
    """
    try:
        with open(SCORES_PATH, "r") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        clean = []
        for e in data:
            if isinstance(e, dict) and "name" in e and "score" in e:
                clean.append({
                    "name": str(e["name"])[:24],
                    "score": int(e["score"]),
                    "mode": str(e.get("mode", "")),
                })
        clean.sort(key=lambda e: e["score"], reverse=True)
        return clean[:MAX_ENTRIES]
    except Exception:
        return []


def save(entries):
    """
    Write the table. Uses a temp file and an atomic rename so a power cut
    mid-write cannot leave a half-written file that fails to parse next boot -
    which on an arcade cabinet is exactly when it would happen.
    """
    try:
        tmp = SCORES_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(entries[:MAX_ENTRIES], f, indent=1)
        os.replace(tmp, SCORES_PATH)
        return True
    except Exception as e:
        print("Could not save high scores: %s" % e)
        return False


def qualifies(score, entries=None):
    """Would this score make the board?"""
    if score <= 0:
        return False
    if entries is None:
        entries = load()
    if len(entries) < MAX_ENTRIES:
        return True
    return score > entries[-1]["score"]


def submit(name, score, mode=""):
    """
    Add an entry and persist. Returns (entries, rank) where rank is the
    1-based position of the new entry, or None if it did not make the board.
    """
    entries = load()
    if not qualifies(score, entries):
        return entries, None
    entry = {"name": name, "score": int(score), "mode": mode}
    entries.append(entry)
    entries.sort(key=lambda e: e["score"], reverse=True)
    entries = entries[:MAX_ENTRIES]
    save(entries)
    rank = None
    for i, e in enumerate(entries):
        if e is entry:
            rank = i + 1
            break
    return entries, rank
