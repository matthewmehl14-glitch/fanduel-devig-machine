"""
Shared helpers for scanner.py and grader.py.

Keeping the CSV schema, odds math and name matching in one place means the
scanner and grader can never drift out of sync with each other.
"""
import csv
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")

CSV_FILENAME = "ev_plays_log.csv"
UNIT_SIZE = 25.00

FIELDNAMES = [
    "Timestamp", "Sport", "Event ID", "Commence Time", "Game", "Market", "Player",
    "Side", "Line", "Bookmaker", "Odds", "True Prob %", "Edge %", "Kelly Units",
    "Bet Amount", "Fair Books", "Close Odds", "Close Fair %", "CLV %",
    "Result", "Net Units", "Actual",
]
GRADED = {"WIN", "LOSS", "PUSH", "VOID"}


# ---------------------------------------------------------------- odds math
def american_to_prob(odds):
    odds = float(odds)
    if odds < 0:
        return -odds / (-odds + 100)
    return 100 / (odds + 100)


def american_to_decimal(odds):
    odds = float(odds)
    if odds > 0:
        return odds / 100 + 1
    return 100 / -odds + 1


def prob_to_american(prob):
    if prob is None or prob <= 0 or prob >= 1:
        return "N/A"
    if prob >= 0.5:
        return str(int(round(-100 * prob / (1 - prob))))
    return f"+{int(round(100 * (1 - prob) / prob))}"


def fmt_american(odds):
    odds = int(round(float(odds)))
    return f"+{odds}" if odds > 0 else str(odds)


def parse_american(s):
    return float(str(s).replace("+", "").strip())


def safe_float(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- time
def parse_iso(s):
    """Parse Odds API / ESPN / CSV timestamps. Naive values are treated as UTC."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- names
def normalize_name(name):
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", str(name)).encode("ASCII", "ignore").decode("utf-8")
    name = name.lower()
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", name)
    name = re.sub(r"[^a-z\s]", "", name)
    return " ".join(name.split())


def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


def _first_names_compatible(a, b):
    # "nic"/"nicolas", "cam"/"cameron", or a near-identical spelling.
    # Rejects look-alikes such as "jalen"/"jaylin" (ratio ~0.73).
    return a.startswith(b) or b.startswith(a) or similarity(a, b) >= 0.8


def match_name(target, candidates):
    """
    Return the candidate that is the same person as `target`, or None.

    Exact normalized match always wins. Otherwise last names must match
    (or nearly), first names must be compatible, and the best match must be
    clearly better than the runner-up. Ambiguity returns None rather than
    guessing, because a wrong match is worse than a missed one.
    """
    tn = normalize_name(target)
    if not tn:
        return None
    tparts = tn.split()
    qualified = []
    for c in candidates:
        cn = normalize_name(c)
        if not cn:
            continue
        if cn == tn or cn.replace(" ", "") == tn.replace(" ", ""):
            return c  # also catches "Gilgeous-Alexander" vs "Gilgeous Alexander", "P.J." vs "PJ"
        cparts = cn.split()
        if len(tparts) < 2 or len(cparts) < 2:
            continue
        last_ok = tparts[-1] == cparts[-1] or similarity(tparts[-1], cparts[-1]) >= 0.85
        if last_ok and _first_names_compatible(tparts[0], cparts[0]):
            qualified.append((similarity(tn, cn), c))
    if not qualified:
        return None
    qualified.sort(key=lambda x: x[0], reverse=True)
    if len(qualified) > 1 and qualified[0][0] - qualified[1][0] < 0.05:
        return None
    return qualified[0][1]


# ---------------------------------------------------------------- csv
def load_rows(path=CSV_FILENAME):
    """Load the play log, migrating any older layout to FIELDNAMES.
    Old '=== RUN ===' separator rows are dropped."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            player = (r.get("Player") or "").strip()
            game = (r.get("Game") or "").strip()
            if not player or player.startswith("---") or game.startswith("==="):
                continue
            row = {k: (r.get(k) or "").strip() for k in FIELDNAMES}
            if row["Result"] not in GRADED:
                row["Result"] = "PENDING"
            if not row["Net Units"]:
                row["Net Units"] = "0.00"
            rows.append(row)
    return rows


def save_rows(rows, path=CSV_FILENAME):
    """Atomic write: a crash mid-write can't leave a half-written log."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def play_key(game, market, player, side, line):
    line_f = safe_float(line)
    line_s = f"{line_f:g}" if line_f is not None else str(line).strip()
    return (
        str(game).strip().lower(),
        str(market).strip().lower(),
        normalize_name(player),
        str(side).strip().lower(),
        line_s,
    )
