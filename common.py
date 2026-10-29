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
UNIT_SIZE = 35.00

FIELDNAMES = [
    "Timestamp", "Sport", "Event ID", "Commence Time", "Game", "Market", "Player",
    "Side", "Line", "Bookmaker", "Odds", "True Prob %", "Edge %", "Kelly Units",
    "Bet Amount", "Fair Books", "Close Odds", "Close Fair %", "CLV %",
    "Result", "Net Units", "Actual",
    # side-by-side test columns
    "Method", "FD Edge %", "Cons Edge %", "Close FD Fair %", "CLV FD %",
]
GRADED = {"WIN", "LOSS", "PUSH", "VOID"}

# ---------------------------------------------------------------- betting rule
# The plays you actually bet: FD-method plays (FD, SOLO, BOTH) with edge >= 5.5%.
# Tracked in the grader only from RULE_START forward, so it's judged on new data.
RULE_MIN_EDGE = 5.5          # percent, compared with the logged "Edge %"
RULE_METHODS = {"fd", "both"}
RULE_START = "2026-10-11"    # first scan date (Central) that counts toward the rule's record


def meets_rule(row):
    try:
        edge = float(row.get("Edge %") or 0)
    except ValueError:
        return False
    return row.get("Method") in RULE_METHODS and edge >= RULE_MIN_EDGE


# ---------------------------------------------------------------- candidate rule (tracking only)
# Tracked side by side with the betting rule from CANDIDATE_START forward. Not starred, not bet.
CANDIDATE_MIN_EDGE = 4.0     # FD edge, percent
CANDIDATE_MIN_CONS = 1.5     # consensus edge, percent (must be present)
CANDIDATE_START = "2026-10-15"


def meets_candidate(row):
    """FD disagreed (FD-method play the consensus priced but didn't flag), edge >= 4%, cons edge >= 1.5%."""
    if row.get("Method") != "fd" or not (row.get("Cons Edge %") or "").strip():
        return False
    try:
        edge = float(row.get("Edge %") or 0)
        cons = float(row.get("Cons Edge %"))
    except ValueError:
        return False
    return edge >= CANDIDATE_MIN_EDGE and cons >= CANDIDATE_MIN_CONS


# ---------------------------------------------------------------- rule 3 (tracking only)
# 🎯 FD plays only (not BOTH) with FD edge 4-8% and odds from -150 to +100. Not starred, not bet.
R3_METHODS = {"fd"}
R3_MIN_EDGE = 4.0            # FD edge, percent (inclusive)
R3_MAX_EDGE = 8.0            # FD edge, percent (inclusive)
R3_MIN_ODDS = -150           # American odds (inclusive): -150 ... -100, +100
R3_MAX_ODDS = 100
R3_START = "2026-10-20"      # live tracking starts
R3_BACKTEST_FROM = "2026-10-05"


def meets_rule3(row):
    if row.get("Method") not in R3_METHODS:
        return False
    try:
        edge = float(row.get("FD Edge %") or row.get("Edge %") or 0)
        odds = float(str(row.get("Odds") or "").replace("+", "").strip())
    except ValueError:
        return False
    if not (R3_MIN_EDGE <= edge <= R3_MAX_EDGE):
        return False
    # American odds skip the gap between -100 and +100, so "between -150 and +100" means
    # -150 <= odds <= -100, or odds of +100 exactly.
    return (R3_MIN_ODDS <= odds <= -100) or (100 <= odds <= R3_MAX_ODDS)


# ---------------------------------------------------------------- moderate dogs (tracking only)
# Odds +126 to +150 with logged edge in the 3.5-4.5% or 6-8% buckets (same buckets as the
# grader's ROI-by-edge table: lower bound included, upper excluded). Any method, current
# scanner only (rows with an Event ID). Not starred, not bet.
DOGS_MIN_ODDS = 126
DOGS_MAX_ODDS = 150
DOGS_EDGE_BANDS = [(3.5, 4.5), (6.0, 8.0)]


def meets_dogs(row):
    if not (row.get("Event ID") or "").strip():
        return False
    try:
        edge = float(row.get("Edge %") or 0)
        odds = float(str(row.get("Odds") or "").replace("+", "").strip())
    except ValueError:
        return False
    if not (DOGS_MIN_ODDS <= odds <= DOGS_MAX_ODDS):
        return False
    return any(lo <= edge < hi for lo, hi in DOGS_EDGE_BANDS)


# ---------------------------------------------------------------- rule 5 (tracking only)
# Novig bets on 🎯 FD plays (method exactly "fd") at odds -120 to -100. Any sport, market or side;
# no extra edge filter beyond what the scanner already requires to log a play. Not starred, not bet.
R5_BOOK = "Novig"
R5_METHODS = {"fd"}
R5_MIN_ODDS = -120           # American odds, inclusive
R5_MAX_ODDS = -100
R5_START = "2026-10-21"      # live tracking starts
R5_BACKTEST_FROM = "2026-10-05"


def meets_rule5(row):
    if row.get("Method") not in R5_METHODS or (row.get("Bookmaker") or "").strip() != R5_BOOK:
        return False
    try:
        odds = float(str(row.get("Odds") or "").replace("+", "").strip())
    except ValueError:
        return False
    return R5_MIN_ODDS <= odds <= R5_MAX_ODDS


# ---------------------------------------------------------------- unders (tracking only)
# Unders on 🎯 FD disagreed plays only (method "fd" AND the consensus priced the prop, so not SOLO,
# not BOTH, not CONS), logged edge 3.00% <= edge < 5.00%, odds -149 to +125,
# excluding the Assists, Total Saves and Threes markets. Current scanner only (rows with an
# Event ID). Not starred, not bet.
UNDERS_METHODS = {"fd"}
UNDERS_MIN_EDGE = 3.0        # inclusive
UNDERS_MAX_EDGE = 5.0        # exclusive
UNDERS_MIN_ODDS = -149       # American odds, inclusive: -149 ... -100, +100 ... +125
UNDERS_MAX_ODDS = 125
UNDERS_EXCLUDE_MARKETS = {"Assists", "Total Saves", "Threes"}   # exact names; "Points Rebounds Assists" still counts
UNDERS_START = "2026-10-29"  # live tracking starts (rule revised Oct 25)
UNDERS_BACKTEST_FROM = "2026-10-05"


def meets_unders(row):
    if not (row.get("Event ID") or "").strip():
        return False
    if row.get("Method") not in UNDERS_METHODS:
        return False
    if not (row.get("Cons Edge %") or "").strip():
        return False  # FD SOLO: the consensus couldn't price it
    if (row.get("Side") or "").strip().lower() != "under":
        return False
    if (row.get("Market") or "").strip() in UNDERS_EXCLUDE_MARKETS:
        return False
    try:
        edge = float(row.get("Edge %") or 0)
        odds = float(str(row.get("Odds") or "").replace("+", "").strip())
    except ValueError:
        return False
    if not (UNDERS_MIN_EDGE <= edge < UNDERS_MAX_EDGE):
        return False
    # American odds skip the gap between -100 and +100.
    return (UNDERS_MIN_ODDS <= odds <= -100) or (100 <= odds <= UNDERS_MAX_ODDS)


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
