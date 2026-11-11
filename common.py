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
UNIT_SIZE = 35.00   # dollars per unit (matches the repo as of Nov 4)

FIELDNAMES = [
    "Timestamp", "Sport", "Event ID", "Commence Time", "Game", "Market", "Player",
    "Side", "Line", "Bookmaker", "Odds", "True Prob %", "Edge %", "Kelly Units",
    "Bet Amount", "Fair Books", "Close Odds", "Close Fair %", "CLV %",
    "Result", "Net Units", "Actual",
    # side-by-side test columns
    "Method", "FD Edge %", "Cons Edge %", "Close FD Fair %", "CLV FD %",
    # when the closing-line snapshot was last taken (UTC); the last pre-game scan is the close
    "Close Time",
    # shadow "study" weights (Novig 7, DK 1.4, FanDuel 1, Caesars 0.5; BetMGM/theScore off).
    # Tracking only: never used to pick, stake, star or mark a play.
    "Study Fair %", "Study Edge %", "Study Books", "Study Close Fair %", "Study CLV %",
    # calibration: True Prob % corrected by how far off the scanner has been for this sport + side
    "Cal Prob %", "Cal Edge %",
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
UNDERS_EXCLUDE_MARKETS = {"Assists", "Total Saves", "Threes",                  # exact names
                          "Rebounds", "Points Rebounds", "Points Rebounds Assists",
                          "Points Assists", "Rebounds Assists"}                # rebounds + combo stats (Oct 31)
UNDERS_START = "2026-10-31"  # live tracking starts (rule revised Oct 31: no rebounds/combos)
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


# ---------------------------------------------------------------- hope (tracking only)
# Every Under (any sport, any method) plus NFL Receptions and Rush Yds Overs; no NFL Reception Yds
# on either side; logged edge 4.5% to 7.0% (both inclusive). Current scanner only (rows with an
# Event ID). Not starred, not bet.
HOPE_MIN_EDGE = 4.5
HOPE_MAX_EDGE = 7.0
HOPE_NFL_OVER_MARKETS = {"Receptions", "Rush Yds"}
HOPE_NFL_CUT_MARKETS = {"Reception Yds"}
HOPE_START = "2026-10-30"    # live tracking starts
HOPE_BACKTEST_FROM = "2026-10-05"


def meets_hope(row):
    if not (row.get("Event ID") or "").strip():
        return False
    nfl = row.get("Sport") == "americanfootball_nfl"
    market = (row.get("Market") or "").strip()
    if nfl and market in HOPE_NFL_CUT_MARKETS:
        return False
    try:
        edge = float(row.get("Edge %") or 0)
    except ValueError:
        return False
    if not (HOPE_MIN_EDGE <= edge <= HOPE_MAX_EDGE):
        return False
    side = (row.get("Side") or "").strip().lower()
    if side == "under":
        return True
    return nfl and side == "over" and market in HOPE_NFL_OVER_MARKETS


# ---------------------------------------------------------------- cha ching (tracking only)
# Two parts, one rule:
#   NBA: Points Unders at -105 or longer (-105 ... -100, or any plus price)
#   NHL: any Under where the scanner's Kelly stake ("Kelly Units") is 1.0u or more, except Total Saves
# Any method. Current scanner only (rows with an Event ID). Not starred, not bet.
CHA_NBA_MARKET = "Points"
CHA_NBA_MIN_ODDS = -105      # American; "-105 or longer" = -105 and anything paying more
CHA_NHL_MIN_KELLY = 1.0      # units
CHA_NHL_EXCLUDE_MARKETS = {"Total Saves"}   # removed Nov 3 (lost in 3 of 4 backtest weeks)
CHA_START = "2026-11-03"     # live tracking starts
CHA_BACKTEST_FROM = "2026-10-05"


def meets_chaching(row):
    if not (row.get("Event ID") or "").strip():
        return False
    if (row.get("Side") or "").strip().lower() != "under":
        return False
    sport = row.get("Sport")
    try:
        if sport == "basketball_nba":
            odds = float(str(row.get("Odds") or "").replace("+", "").strip())
            return (row.get("Market") or "").strip() == CHA_NBA_MARKET and odds >= CHA_NBA_MIN_ODDS
        if sport == "icehockey_nhl":
            if (row.get("Market") or "").strip() in CHA_NHL_EXCLUDE_MARKETS:
                return False
            return float(row.get("Kelly Units") or 0) >= CHA_NHL_MIN_KELLY
    except ValueError:
        return False
    return False


# ---------------------------------------------------------------- calienta (tracking only)
# Two parts, one rule:
#   NHL: Shots On Goal Unders, logged edge >= 4%, method BOTH or FD (FD includes disagreed and SOLO)
#   NBA: Points Unders, method BOTH (any edge the scanner logs, any odds)
# Current scanner only (rows with an Event ID). Not starred, not bet.
CAL_NHL_MARKET = "Shots On Goal"
CAL_NHL_MIN_EDGE = 4.0
CAL_NHL_METHODS = {"both", "fd"}
CAL_NBA_MARKET = "Points"
CAL_NBA_METHODS = {"both"}
CAL_START = "2026-11-07"     # live tracking starts
CAL_BACKTEST_FROM = "2026-10-05"


def meets_calienta(row):
    if not (row.get("Event ID") or "").strip():
        return False
    if (row.get("Side") or "").strip().lower() != "under":
        return False
    sport, market, method = row.get("Sport"), (row.get("Market") or "").strip(), row.get("Method")
    if sport == "icehockey_nhl":
        try:
            edge = float(row.get("Edge %") or 0)
        except ValueError:
            return False
        return market == CAL_NHL_MARKET and method in CAL_NHL_METHODS and edge >= CAL_NHL_MIN_EDGE
    if sport == "basketball_nba":
        return market == CAL_NBA_MARKET and method in CAL_NBA_METHODS
    return False


# ---------------------------------------------------------------- WTF (tracking only)
# "Configuration A": sport-specific windows on odds, hours before start, method and edge.
# Either side (Overs and Unders). Hours before start = Commence Time (UTC) minus Timestamp (Central).
#   NHL: no Total Saves | odds -139..+139 | found <=3h or >=9h before start | fd or both | edge 5.0-7.5%
#   NBA: no Assists     | odds +100..+199 | found 9-12h before start        | fd or consensus | edge 4.5-7.5%
#   NFL:                | odds -139..+139 | found 3-6h before start         | fd or consensus | edge >= 4.0%
# All bounds inclusive. Not starred, not bet.
WTF_START = "2026-11-07"     # live tracking starts
WTF_BACKTEST_FROM = "2026-10-15"


def hours_before_start(row):
    """Hours between when a play was logged (Central) and game start (UTC). None if unknown."""
    try:
        logged = datetime.strptime((row.get("Timestamp") or "")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CT)
    except ValueError:
        return None
    start = parse_iso(row.get("Commence Time"))
    return (start - logged).total_seconds() / 3600 if start else None


def meets_wtf(row):
    sport, market, method = row.get("Sport"), (row.get("Market") or "").strip(), row.get("Method")
    if sport not in ("icehockey_nhl", "basketball_nba", "americanfootball_nfl"):
        return False
    try:
        odds = float(str(row.get("Odds") or "").replace("+", "").strip())
        edge = float(row.get("Edge %") or "")
    except ValueError:
        return False
    h = hours_before_start(row)
    if h is None:
        return False
    if sport == "icehockey_nhl":
        return (market != "Total Saves" and -139 <= odds <= 139 and (h <= 3.0 or h >= 9.0)
                and method in ("fd", "both") and 5.0 <= edge <= 7.5)
    if sport == "basketball_nba":
        return (market != "Assists" and 100 <= odds <= 199 and 9.0 <= h <= 12.0
                and method in ("fd", "consensus") and 4.5 <= edge <= 7.5)
    return -139 <= odds <= 139 and 3.0 <= h <= 6.0 and method in ("fd", "consensus") and edge >= 4.0


# ---------------------------------------------------------------- lucky charm (tracking only)
# Three filters, then one of three sport windows. "Edge" is the logged Edge %; hours out as in WTF.
# Step 1  Sharp consensus: if Fair Books lists 3+ books, at least 2 must be sharp
#         (FanDuel, DraftKings, Novig, Pinnacle). 1-2 book fair lines pass.
# Step 2  fd guardrail: for Method "fd", Cons Edge % (if filled) >= 2.0 and Study Edge % (if filled) >= 2.0.
# Step 3  No Total Saves. Overs must clear a 1.5-point haircut on True Prob %:
#         ((True Prob % - 1.5) / 100 * decimal odds - 1) * 100 >= 3.5
# NBA: Unders only; no Rebounds or Threes; odds -139..+114; >=9h or <=6h out; edge >= 4.5%
# NHL: Shots On Goal; Under or haircut-passing Over; odds -139..+139; >=6h or <=3h out; edge 4.0-7.5%
# NFL: any market; Under or haircut-passing Over; odds -139..+139; 1-6h out; fd or consensus; edge 3.5-7.5%
# All bounds inclusive. Not starred, not bet.
LUCKY_SHARP_BOOKS = {"FanDuel", "DraftKings", "Novig", "Pinnacle"}
LUCKY_FLOOR = 2.0            # Step 2 floor for Cons Edge % and Study Edge %, percent
LUCKY_OVER_HAIRCUT = 1.5     # Step 3, percentage points off True Prob %
LUCKY_OVER_MIN_EDGE = 3.5    # Step 3, adjusted edge needed for an Over
LUCKY_START = "2026-11-07"   # live tracking starts
LUCKY_BACKTEST_FROM = "2026-10-15"


def lucky_over_adjusted_edge(row):
    try:
        p = float(row.get("True Prob %") or "")
        odds = float(str(row.get("Odds") or "").replace("+", "").strip())
    except ValueError:
        return None
    dec = odds / 100 + 1 if odds > 0 else 100 / -odds + 1
    return ((p - LUCKY_OVER_HAIRCUT) / 100 * dec - 1) * 100


def meets_lucky(row):
    sport, market, method = row.get("Sport"), (row.get("Market") or "").strip(), row.get("Method")
    side = (row.get("Side") or "").strip().lower()
    if sport not in ("basketball_nba", "icehockey_nhl", "americanfootball_nfl"):
        return False
    try:
        odds = float(str(row.get("Odds") or "").replace("+", "").strip())
        edge = float(row.get("Edge %") or "")
    except ValueError:
        return False
    # Step 1: sharp consensus
    books = [b.strip() for b in (row.get("Fair Books") or "").split(",") if b.strip()]
    if len(books) >= 3 and sum(b in LUCKY_SHARP_BOOKS for b in books) < 2:
        return False
    # Step 2: fd guardrail
    if method == "fd":
        for col in ("Cons Edge %", "Study Edge %"):
            v = (row.get(col) or "").strip()
            if v:
                try:
                    if float(v) < LUCKY_FLOOR:
                        return False
                except ValueError:
                    return False
    # Step 3: market cleanup + Over haircut
    if market == "Total Saves":
        return False
    if side == "over":
        adj = lucky_over_adjusted_edge(row)
        if adj is None or adj < LUCKY_OVER_MIN_EDGE:
            return False
    elif side != "under":
        return False
    h = hours_before_start(row)
    if h is None:
        return False
    if sport == "basketball_nba":
        return (side == "under" and market not in ("Rebounds", "Threes") and -139 <= odds <= 114
                and (h >= 9.0 or h <= 6.0) and edge >= 4.5)
    if sport == "icehockey_nhl":
        return (market == "Shots On Goal" and -139 <= odds <= 139 and (h >= 6.0 or h <= 3.0)
                and 4.0 <= edge <= 7.5)
    return (-139 <= odds <= 139 and 1.0 <= h <= 6.0 and method in ("fd", "consensus")
            and 3.5 <= edge <= 7.5)


# ---------------------------------------------------------------- calibration
# The scanner's win chances run a few points high. Every scan refits a correction from the last
# CAL_WINDOW_DAYS of graded plays: for each sport + side, (actual win rate - predicted win rate),
# pulled toward the overall gap for small groups (weight n / (n + CAL_SHRINK)). The corrected
# edge is logged as "Cal Edge %". Display and tracking only: it never changes what gets logged.
CAL_WINDOW_DAYS = 28
CAL_SHRINK = 40              # plays; how strongly small groups lean on the overall gap
CAL_MIN_EDGE = 3.5           # a play "passes calibration" at Cal Edge % >= this
CAL_TRACK_START = "2026-11-11"
CAL_SPORTS = ("icehockey_nhl", "basketball_nba", "americanfootball_nfl")


def fit_calibration(rows, today_iso):
    """Returns {"global": pts, (sport, side): pts, "_n": {...}} in percentage points of win chance."""
    from datetime import date, timedelta as _td
    end = date.fromisoformat(today_iso)
    start = (end - _td(days=CAL_WINDOW_DAYS)).isoformat()
    use = []
    for r in rows:
        d = (r.get("Timestamp") or "")[:10]
        if not (start <= d < today_iso) or r.get("Result") not in ("WIN", "LOSS") or not r.get("Method"):
            continue
        try:
            p = float(r.get("True Prob %") or "")
        except ValueError:
            continue
        use.append((r.get("Sport"), (r.get("Side") or "").strip(), p, 100.0 if r["Result"] == "WIN" else 0.0))
    cal = {"global": 0.0, "_n": {}}
    if not use:
        return cal
    glob = sum(w - p for _, _, p, w in use) / len(use)
    cal["global"] = glob
    groups = {}
    for sport, side, p, w in use:
        groups.setdefault((sport, side), []).append(w - p)
    for key, gaps in groups.items():
        n = len(gaps)
        wt = n / (n + CAL_SHRINK)
        cal[key] = wt * (sum(gaps) / n) + (1 - wt) * glob
        cal["_n"][key] = n
    return cal


def calibrated_prob_edge(row, cal):
    """(calibrated prob %, calibrated edge %) for a row, or (None, None)."""
    try:
        p = float(row.get("True Prob %") or "")
        odds = float(str(row.get("Odds") or "").replace("+", "").strip())
    except ValueError:
        return None, None
    adj = cal.get((row.get("Sport"), (row.get("Side") or "").strip()), cal.get("global", 0.0))
    cp = min(98.0, max(2.0, p + adj))
    dec = odds / 100 + 1 if odds > 0 else 100 / -odds + 1
    return cp, (cp / 100 * dec - 1) * 100


def passes_calibration(row):
    try:
        return float(row.get("Cal Edge %") or "") >= CAL_MIN_EDGE
    except ValueError:
        return False


# ---------------------------------------------------------------- study weights (shadow)
# "The study agrees" = the study-weight edge is at least the scanner's own minimum edge.
STUDY_AGREE_EDGE = 3.5   # percent


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
