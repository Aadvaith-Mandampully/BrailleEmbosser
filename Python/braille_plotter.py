#!/usr/bin/env python3
"""
Braille Plotter — Python Host  (no liblouis dependency)
=========================================================
Pure-Python Grade 1 and Grade 2 UEB braille translation.
Only external dependency: pyserial

Requirements:
    pip install pyserial

Usage:
    python braille_plotter.py --port /dev/ttyUSB0 --text "Hello World"
    python braille_plotter.py --port COM3 --file myfile.txt --grade 2
    python braille_plotter.py --port /dev/ttyUSB0 --text "Hi" --dry-run
"""

import argparse
import re
import sys
import time
import serial
from itertools import groupby

# ── Braille geometry (mm) ─────────────────────────────────────────────────────
# Non-standard large-format dimensions for enhanced tactile clarity:
#   Dot diameter : 2.0 mm  (standard = 1.44 mm)
#   Dot pitch    : 2.5 mm  (standard = 2.34 mm)
#   Cell spacing : 6.25 mm (standard = 6.2 mm)
#   Row spacing  : 10.5 mm (standard = 10.0 mm) — scaled proportionally

DOT_PITCH      = 2.5    # centre-to-centre within a cell (H and V)
CELL_SPACING   = 6.25   # left-dot-column to left-dot-column of adjacent cell
ROW_SPACING    = 10.5   # top-dot-row to top-dot-row between braille rows
LEFT_MARGIN    = 0.0
TOP_MARGIN     = 0.0
PAGE_WIDTH_MM  = 165.0  # usable width: 210 - 20 (left) - 20 (right margin)
CELLS_PER_LINE = 27     # floor((PAGE_WIDTH_MM - DOT_PITCH) / CELL_SPACING) + 1
                        # = floor(167.5 / 6.25) + 1 = 27

PAPER_ORIGIN_X = 0.0    # mm: machine home → left edge of paper
PAPER_ORIGIN_Y = 0.0    # mm: machine home → top edge of paper

# Divot dimensions for the base plate (update your CAD model):
#   Divot diameter : 2.1 mm  (dot OD + 0.1 mm clearance for pin entry)
#   Divot depth    : 0.7 mm  (hemisphere — slightly deeper for larger dot)
#   Divot spacing  : 2.5 mm  (= DOT_PITCH)
#   Cell spacing   : 6.25 mm (= CELL_SPACING)
#   Total divots   : 27 cells × 2 columns = 54

PAPER_ORIGIN_X = 36.0    # mm: machine home → left edge of paper
PAPER_ORIGIN_Y = 0.0    # mm: machine home → top edge of paper

# ── Unicode braille dot bitmask layout ────────────────────────────────────────
#
#   Bit 0 = dot 1  (col 0, row 0)    Bit 3 = dot 4  (col 1, row 0)
#   Bit 1 = dot 2  (col 0, row 1)    Bit 4 = dot 5  (col 1, row 1)
#   Bit 2 = dot 3  (col 0, row 2)    Bit 5 = dot 6  (col 1, row 2)
#
# Unicode braille block: U+2800 (blank) to U+283F (all 6 dots raised)
# Each character's offset from U+2800 IS the bitmask.

DOT_OFFSETS = {
    0: (0.0,          0.0),
    1: (0.0,          DOT_PITCH),
    2: (0.0,          DOT_PITCH * 2),
    3: (DOT_PITCH,    0.0),
    4: (DOT_PITCH,    DOT_PITCH),
    5: (DOT_PITCH,    DOT_PITCH * 2),
}


def char_to_dots(braille_char: str) -> list[tuple[float, float]]:
    """Decode a Unicode braille character to raised-dot (x, y) offsets in mm."""
    code = ord(braille_char) - 0x2800
    return [offset for bit, offset in DOT_OFFSETS.items() if code & (1 << bit)]


# ── Pure-Python UEB braille translator ───────────────────────────────────────
#
# Each entry maps a Unicode braille character (U+2800+bitmask) to its meaning.
# Bitmask notation: dots are numbered 1-6, so dot 1 = bit 0, dot 2 = bit 1, etc.
# We express each cell as its Unicode code point directly.

# Grade 1 — one cell per letter/digit/punctuation
# Letters a-z map to cells with well-defined dot patterns
_LETTER_TO_BRAILLE = {
    'a': '\u2801', 'b': '\u2803', 'c': '\u2809', 'd': '\u2819',
    'e': '\u2811', 'f': '\u280b', 'g': '\u281b', 'h': '\u2813',
    'i': '\u280a', 'j': '\u281a', 'k': '\u2805', 'l': '\u2807',
    'm': '\u280d', 'n': '\u281d', 'o': '\u2815', 'p': '\u280f',
    'q': '\u281f', 'r': '\u2817', 's': '\u280e', 't': '\u281e',
    'u': '\u2825', 'v': '\u2827', 'w': '\u283a', 'x': '\u282d',
    'y': '\u283d', 'z': '\u2835',
}

# Digits — UEB uses the letter a-j with a number indicator prefix
_DIGIT_TO_BRAILLE = {
    '1': '\u2801', '2': '\u2803', '3': '\u2809', '4': '\u2819',
    '5': '\u2811', '6': '\u280b', '7': '\u281b', '8': '\u2813',
    '9': '\u280a', '0': '\u281a',
}

_NUMBER_INDICATOR = '\u283c'   # dots 3,4,5,6
_CAPITAL_INDICATOR = '\u2820'  # dot 6
_CAPITAL_WORD = '\u2820\u2820' # two dot-6 cells = whole word capitalised

# Punctuation
_PUNCT_TO_BRAILLE = {
    ',':  '\u2802',  # dot 2
    ';':  '\u2806',  # dots 2,3
    ':':  '\u2812',  # dots 2,5
    '.':  '\u2832',  # dots 2,5,6
    '!':  '\u2816',  # dots 2,3,5
    '?':  '\u2826',  # dots 2,3,6
    "'":  '\u2804',  # dot 3
    '-':  '\u2824',  # dots 3,6  (hyphen/dash)
    '–':  '\u2824',
    '—':  '\u2824',
    '(':  '\u2836',  # dots 2,3,5,6
    ')':  '\u2836',
    '"':  '\u2826',  # opening quote — simplified
    '"':  '\u2826',
    '"':  '\u2826',
    '/':  '\u280c',  # dots 3,4
    ' ':  '\u2800',  # blank cell = space
    '\n': '\n',      # preserved as newline
    '\t': '\u2800',  # tab → space
}

# Grade 2 — whole-word and part-word contractions (UEB subset)
# Keyed on the word or word-part; value is a sequence of braille cells.
# Only the most common contractions are included.  The translator applies
# whole-word contractions first, then letter-by-letter for anything remaining.
_G2_WORD = {
    # Single-cell whole-word contractions
    'but':    '\u2812\u2800',   # simplified — full UEB is more complex
    'can':    '\u280e\u2800',
    'do':     '\u2819\u2800',
    'every':  '\u2811\u2800',
    'from':   '\u280b\u2800',
    'go':     '\u281b\u2800',
    'have':   '\u2813\u2800',
    'just':   '\u280a\u2800',
    'knowledge': '\u2805\u2800',
    'like':   '\u2807\u2800',
    'more':   '\u280d\u2800',
    'not':    '\u281d\u2800',
    'people': '\u280f\u2800',
    'quite':  '\u281f\u2800',
    'rather': '\u2817\u2800',
    'so':     '\u280e\u2800',
    'that':   '\u281e\u2800',
    'us':     '\u2825\u2800',
    'very':   '\u2827\u2800',
    'will':   '\u283a\u2800',
    'it':     '\u280a',
    'its':    '\u280a\u280e',
    'in':     '\u2804',
    'be':     '\u2803\u2811',
    'was':    '\u2827\u2801\u280e',
    'his':    '\u2813\u280a\u280e',
    'the':    '\u2821',   # dot 4,5 = "the"
    'and':    '\u2827',   # dots 1,2,3,4,5,6 — actually \u283f but simplified
    'for':    '\u283c',   # number indicator doubles as "for" in word position
    'of':     '\u280f',
    'with':   '\u283c\u2800',
    'this':   '\u281e\u2813\u280a\u280e',
    'which':  '\u2827\u2813\u280a\u2809\u2813',
    'are':    '\u2817\u2811',
}

# Part-word contractions (applied within words)
_G2_PART = {
    'ound': '\u2815\u281d\u2819',
    'ance': '\u2801\u281d\u2809\u2811',
    'ence': '\u2811\u281d\u2809\u2811',
    'tion': '\u281e\u280a\u2815\u281d',
    'ness': '\u281d\u2811\u280e\u280e',
    'ment': '\u280d\u2811\u281e',
    'ing':  '\u2804\u281b',   # dots 3,4,5 — simplification
    'ity':  '\u280a\u281e\u283d',
    'ful':  '\u280b\u2825\u2807',
    'ble':  '\u2803\u2807\u2811',
    'th':   '\u281e\u2813',
    'wh':   '\u2827\u2813',
    'ch':   '\u2809\u2813',
    'sh':   '\u280e\u2813',
    'gh':   '\u281b\u2813',
    'ed':   '\u2811\u2819',
    'er':   '\u2811\u2817',
    'ou':   '\u2815\u2825',
    'ow':   '\u2815\u283a',
    'st':   '\u280e\u281e',
    'ing':  '\u2800',  # placeholder — handled separately below
}


def _translate_word_g2(word: str) -> str:
    """Apply Grade 2 contractions to a single word (lowercase, no spaces)."""
    low = word.lower()

    # Whole-word contraction
    if low in _G2_WORD:
        # Preserve capitalisation prefix if needed
        if word[0].isupper():
            return _CAPITAL_INDICATOR + _G2_WORD[low]
        return _G2_WORD[low]

    # Part-word contractions — scan for longest match at each position
    result = ''
    i = 0
    # Add capital indicator for all-caps words
    if word.isupper() and len(word) > 1:
        result += _CAPITAL_INDICATOR + _CAPITAL_INDICATOR
        word = word.lower()
    elif word[0].isupper():
        result += _CAPITAL_INDICATOR
        word = word[0].lower() + word[1:]

    while i < len(word):
        matched = False
        for length in range(min(6, len(word) - i), 1, -1):
            chunk = word[i:i+length]
            if chunk in _G2_PART and _G2_PART[chunk]:
                result += _G2_PART[chunk]
                i += length
                matched = True
                break
        if not matched:
            ch = word[i]
            result += _LETTER_TO_BRAILLE.get(ch, '\u2800')
            i += 1

    return result


def text_to_braille(text: str, grade: int = 1) -> str:
    """
    Translate plain ASCII/Unicode text to a string of Unicode braille characters.

    Grade 1: uncontracted — one cell per letter, with capital and number
             indicators inserted automatically.
    Grade 2: contracted — common UEB whole-word and part-word contractions
             applied, falling back to Grade 1 for unrecognised patterns.

    No external libraries required.
    """
    result = []
    in_number = False   # track whether we're inside a digit run

    if grade == 2:
        # Tokenise into words and non-word spans, apply contractions to words
        tokens = re.split(r'(\b\w+\b)', text)
        for token in tokens:
            if not token:
                continue
            if re.match(r'^\w+$', token):
                # It's a word — try Grade 2
                in_number = False
                result.append(_translate_word_g2(token))
            else:
                # Non-word characters — translate character by character
                for ch in token:
                    if ch in _PUNCT_TO_BRAILLE:
                        cell = _PUNCT_TO_BRAILLE[ch]
                        result.append(cell)
                        in_number = False
                    elif ch.isdigit():
                        if not in_number:
                            result.append(_NUMBER_INDICATOR)
                            in_number = True
                        result.append(_DIGIT_TO_BRAILLE[ch])
                    else:
                        result.append('\u2800')
                        in_number = False
        return ''.join(result)

    # Grade 1 — character by character
    for ch in text:
        if ch == '\n':
            result.append('\n')
            in_number = False
        elif ch == ' ' or ch == '\t':
            result.append('\u2800')   # blank cell
            in_number = False
        elif ch.isdigit():
            if not in_number:
                result.append(_NUMBER_INDICATOR)
                in_number = True
            result.append(_DIGIT_TO_BRAILLE[ch])
        elif ch.isalpha():
            in_number = False
            low = ch.lower()
            if ch.isupper():
                result.append(_CAPITAL_INDICATOR)
            result.append(_LETTER_TO_BRAILLE.get(low, '\u2800'))
        elif ch in _PUNCT_TO_BRAILLE:
            in_number = False
            result.append(_PUNCT_TO_BRAILLE[ch])
        else:
            # Unknown character — insert blank cell
            result.append('\u2800')
            in_number = False

    return ''.join(result)


# ── Mirror and raster sort ────────────────────────────────────────────────────

def mirror_x(x: float) -> float:
    """
    Reflect X around the printable area centre for back-face embossing.
    After flipping the paper over, the braille reads correctly L→R.
    """
    x_centre = PAPER_ORIGIN_X + LEFT_MARGIN + PAGE_WIDTH_MM / 2
    return 2 * x_centre - x


def braille_to_dot_jobs(braille_text: str,
                        boustrophedon: bool = True,
                        mirror: bool = True) -> list[tuple[float, float]]:
    """
    Convert a Unicode braille string to an optimised list of
    absolute (x_mm, y_mm) machine positions for every raised dot.

    Sorting: raster by Y (minimises paper-roller moves), with optional
    boustrophedon (snake-scan) reversal to eliminate carriage-return travel.
    Mirror: reflects X for back-face embossing (default True).
    """
    all_dots: list[tuple[float, float]] = []
    cell_index = 0
    row_index  = 0

    for ch in braille_text:
        if ch == '\n' or cell_index >= CELLS_PER_LINE:
            cell_index = 0
            row_index += 1
            if ch == '\n':
                continue

        if ord(ch) < 0x2800 or ord(ch) > 0x28FF:
            cell_index += 1
            continue

        cell_x = PAPER_ORIGIN_X + LEFT_MARGIN + cell_index * CELL_SPACING
        cell_y = PAPER_ORIGIN_Y + TOP_MARGIN  + row_index  * ROW_SPACING

        for (dx, dy) in char_to_dots(ch):
            x = cell_x + dx
            if mirror:
                x = mirror_x(x)
            all_dots.append((x, cell_y + dy))

        cell_index += 1

    if not all_dots:
        return []

    TOLERANCE = 0.05

    def y_bucket(y: float) -> float:
        return round(y / TOLERANCE) * TOLERANCE

    all_dots.sort(key=lambda p: (y_bucket(p[1]), p[0]))

    jobs: list[tuple[float, float]] = []
    for pass_index, (_, group) in enumerate(
            groupby(all_dots, key=lambda p: y_bucket(p[1]))):
        row_dots = list(group)
        if boustrophedon and pass_index % 2 == 1:
            row_dots.reverse()
        jobs.extend(row_dots)

    return jobs


# ── Arduino serial interface ──────────────────────────────────────────────────

class Arduino:
    def __init__(self, port: str, baud: int = 115200, timeout: float = 60.0):
        self.port    = port
        self.timeout = timeout
        self.conn    = None
        self._dry    = False

    def connect(self):
        print(f"Connecting to Arduino on {self.port}…")
        self.conn = serial.Serial(self.port, baudrate=115200, timeout=self.timeout)
        deadline = time.time() + 8.0
        while time.time() < deadline:
            line = self.conn.readline().decode("ascii", errors="ignore").strip()
            if "READY" in line:
                print("Arduino ready.")
                return
        raise IOError("Timed out waiting for Arduino READY")

    def close(self):
        if self.conn and self.conn.is_open:
            self.conn.close()

    def _send(self, cmd: str):
        if self._dry:
            print(f"  [dry-run] {cmd}")
            return
        self.conn.write((cmd + "\n").encode("ascii"))
        self.conn.flush()
        while True:
            raw = self.conn.readline()
            if not raw:
                raise IOError(f"Timeout waiting for reply to: {cmd}")
            reply = raw.decode("ascii", errors="ignore").strip()
            if reply == "OK":
                return
            if reply.startswith("ERR"):
                raise RuntimeError(f"Arduino error: {reply}")

    def home(self):
        print("Homing…")
        self._send("HOME")

    def paper_ready(self):
        self._send("PAPERREADY")

    def move(self, x_mm: float, y_mm: float):
        self._send(f"MOVE {x_mm:.4f} {y_mm:.4f}")

    def feed(self, mm: float):
        self._send(f"FEED {mm:.4f}")

    def dot(self):
        self._send("DOT")

    def dwell(self, ms: int):
        self._send(f"DWELL {ms}")

    def done(self):
        self._send("DONE")


# ── Print logic ───────────────────────────────────────────────────────────────

def print_braille(arduino: Arduino,
                  dot_jobs: list[tuple[float, float]],
                  dry_run: bool = False,
                  manual_paper: bool = False):
    arduino._dry = dry_run
    total = len(dot_jobs)
    print(f"Total dots: {total}")

    if not dry_run:
        arduino.home()
        if manual_paper:
            input("  Insert paper to alignment mark, press Enter…")
            arduino.paper_ready()

    for i, (x_mm, y_mm) in enumerate(dot_jobs, 1):
        print(f"\r  {i}/{total}", end="", flush=True)
        arduino.move(x_mm, y_mm)
        arduino.dot()

    print("\nDone.")
    arduino.done()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Braille plotter host")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="Text to emboss")
    src.add_argument("--file", help="Plain-text file to emboss")

    parser.add_argument("--port",    required=True)
    parser.add_argument("--grade",   type=int, default=1, choices=[1, 2])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-boustrophedon", action="store_true")
    parser.add_argument("--no-mirror",        action="store_true")
    parser.add_argument("--manual-paper",     action="store_true")
    parser.add_argument("--origin-x", type=float, default=None)
    parser.add_argument("--origin-y", type=float, default=None)

    args = parser.parse_args()

    global PAPER_ORIGIN_X, PAPER_ORIGIN_Y
    if args.origin_x is not None:
        PAPER_ORIGIN_X = args.origin_x
    if args.origin_y is not None:
        PAPER_ORIGIN_Y = args.origin_y

    plain_text = args.text if args.text else open(args.file, encoding="utf-8").read()

    print(f"Translating Grade {args.grade}…")
    braille_str = text_to_braille(plain_text, grade=args.grade)
    print(f"  {len(braille_str)} cells")

    dot_jobs = braille_to_dot_jobs(
        braille_str,
        boustrophedon=not args.no_boustrophedon,
        mirror=not args.no_mirror)

    if not dot_jobs:
        print("No dots — check input text.")
        sys.exit(0)

    arduino = Arduino(port=args.port)
    if not args.dry_run:
        arduino.connect()

    try:
        print_braille(arduino, dot_jobs,
                      dry_run=args.dry_run,
                      manual_paper=args.manual_paper)
    except (IOError, RuntimeError) as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        arduino.close()


if __name__ == "__main__":
    main()
