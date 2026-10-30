"""
EV auto-grader.

- Finds the specific game each play was on (teams + start time), not
  "any box score from the last 3 days".
- Combo props (P+R, PRA, NHL points) sum every component.
- DNP / not in box score / postponed -> VOID instead of PENDING forever.
- Reports CLV per edge tier, side and book.

Run `python grader.py --regrade` (or set REGRADE=1) once to re-grade the
whole log with the fixed logic.
"""
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from common import (
    ET, GRADED, RULE_MIN_EDGE, RULE_START, meets_rule,
    CANDIDATE_MIN_CONS, CANDIDATE_MIN_EDGE, CANDIDATE_START, meets_candidate,
    DOGS_EDGE_BANDS, DOGS_MAX_ODDS, DOGS_MIN_ODDS, meets_dogs,
    UNDERS_BACKTEST_FROM, UNDERS_MAX_EDGE, UNDERS_MAX_ODDS, UNDERS_MIN_EDGE, UNDERS_MIN_ODDS, UNDERS_START,
    meets_unders,
    HOPE_BACKTEST_FROM, HOPE_MAX_EDGE, HOPE_MIN_EDGE, HOPE_START, meets_hope,
    R5_BACKTEST_FROM, R5_BOOK, R5_MAX_ODDS, R5_MIN_ODDS, R5_START, meets_rule5,
    R3_BACKTEST_FROM, R3_MAX_EDGE, R3_MAX_ODDS, R3_MIN_EDGE, R3_MIN_ODDS, R3_START, meets_rule3, UNIT_SIZE, american_to_decimal, load_rows, match_name,
    normalize_name, parse_american, parse_iso, safe_float, save_rows, similarity,
)

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
ESPN = "https://site.api.espn.com/apis/site/v2/sports"
MAX_AGE_DAYS = 14   # stop retrying pending plays older than this (unless regrading)

ESPN_LEAGUES = {
    "basketball_nba": ("basketball", "nba"),
    "basketball_nba_preseason": ("basketball", "nba"),
    "basketball_wnba": ("basketball", "wnba"),
    "icehockey_nhl": ("hockey", "nhl"),
    "icehockey_nhl_preseason": ("hockey", "nhl"),
    "americanfootball_nfl": ("football", "nfl"),
    "americanfootball_ncaaf": ("football", "college-football"),
}

# For old rows logged before the Sport column existed
_NBA, _WNBA, _NHL = ("basketball", "nba"), ("basketball", "wnba"), ("hockey", "nhl")
_NFL, _CFB = ("football", "nfl"), ("football", "college-football")
MARKET_FALLBACK_LEAGUES = {
    "Points": [_NBA, _WNBA, _NHL], "Assists": [_NBA, _WNBA, _NHL],
    "Rebounds": [_NBA, _WNBA], "Points Rebounds": [_NBA, _WNBA],
    "Points Rebounds Assists": [_NBA, _WNBA], "Threes": [_NBA, _WNBA],
    "Shots On Goal": [_NHL], "Total Saves": [_NHL],
    "Pass Yds": [_NFL, _CFB], "Pass Attempts": [_NFL, _CFB], "Rush Yds": [_NFL, _CFB],
    "Rush Attempts": [_NFL, _CFB], "Reception Yds": [_NFL, _CFB], "Receptions": [_NFL, _CFB],
}

# market -> list of recipes (first one that fully resolves wins).
# recipe -> list of components that are SUMMED.
# component -> (stat table or None, alternative labels for the same stat)
STAT_MAP = {
    "basketball": {
        "Points": [[(None, ("PTS",))]],
        "Rebounds": [[(None, ("REB",))]],
        "Assists": [[(None, ("AST",))]],
        "Points Rebounds": [[(None, ("PTS",)), (None, ("REB",))]],
        "Points Rebounds Assists": [[(None, ("PTS",)), (None, ("REB",)), (None, ("AST",))]],
        "Threes": [[(None, ("3PT", "3PM"))]],
    },
    "hockey": {
        "Points": [[(None, ("PTS", "P"))], [(None, ("G",)), (None, ("A",))]],
        "Assists": [[(None, ("A", "AST"))]],
        # ESPN NHL: "S" = shots on goal. "SOG" is a shootout column (zero almost always).
        "Shots On Goal": [[(None, ("S",))]],
        "Total Saves": [[(None, ("SV", "SAVES"))]],
    },
    "football": {
        "Pass Yds": [[("passing", ("YDS",))]],
        "Pass Attempts": [[("passing", ("C/ATT", "ATT"))]],
        "Rush Yds": [[("rushing", ("YDS",))]],
        "Rush Attempts": [[("rushing", ("CAR", "ATT"))]],
        "Reception Yds": [[("receiving", ("YDS",))]],
        "Receptions": [[("receiving", ("REC",))]],
    },
}

SESSION = requests.Session()
_scoreboards, _summaries = {}, {}


# ------------------------------------------------------------------ ESPN
def _get(url):
    try:
        res = SESSION.get(url, timeout=12)
        return res.json() if res.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def scoreboard(sport, league, ymd):
    key = (sport, league, ymd)
    if key not in _scoreboards:
        base = f"{ESPN}/{sport}/{league}/scoreboard?dates={ymd}&limit=300"
        urls = [f"{base}&groups=80", f"{base}&groups=81"] if league == "college-football" else [base]
        events, seen = [], set()
        for url in urls:
            for ev in (_get(url) or {}).get("events", []):
                if ev.get("id") not in seen:
                    seen.add(ev.get("id"))
                    events.append(ev)
        _scoreboards[key] = events
    return _scoreboards[key]


def boxscore(sport, league, event_id):
    key = (sport, league, event_id)
    if key not in _summaries:
        data = _get(f"{ESPN}/{sport}/{league}/summary?event={event_id}")
        _summaries[key] = (data or {}).get("boxscore")
    return _summaries[key]


def _team_variants(team):
    return [v for v in (
        team.get("displayName", ""),
        f"{team.get('location', '')} {team.get('name', '')}",
        team.get("shortDisplayName", ""),
    ) if v.strip()]


def _team_score(name, team):
    n = normalize_name(name)
    # Nickname match: ESPN shortens some cities ("LA Clippers") while the Odds API spells them out
    # ("Los Angeles Clippers"). Nicknames are unique within a league, and both teams must match.
    nick = normalize_name(team.get("name", ""))
    if nick and (n == nick or n.endswith(" " + nick)):
        return 1.0
    return max((similarity(n, normalize_name(v)) for v in _team_variants(team)), default=0.0)


def find_game(game_str, events, anchor, exact_start):
    """Match 'Away @ Home' to an ESPN event, using start time to separate
    back-to-backs / same-opponent series."""
    if " @ " not in game_str:
        return None
    away, home = game_str.split(" @ ", 1)
    best, best_key = None, None
    for ev in events:
        comps = (ev.get("competitions") or [{}])[0].get("competitors", [])
        if len(comps) != 2:
            continue
        t0, t1 = comps[0].get("team", {}), comps[1].get("team", {})
        pairs = [(_team_score(away, t0), _team_score(home, t1)),
                 (_team_score(away, t1), _team_score(home, t0))]
        a, h = max(pairs, key=lambda x: x[0] + x[1])
        if min(a, h) < 0.75:
            continue
        start = parse_iso(ev.get("date"))
        if start is None:
            continue
        if exact_start:
            gap = abs((start - anchor).total_seconds())
            if gap > 6 * 3600:
                continue
        else:  # old rows: bet logged before the game, game within ~36h
            if not (anchor - timedelta(hours=2) <= start <= anchor + timedelta(hours=36)):
                continue
            gap = (start - anchor).total_seconds()
        key = (-(a + h), abs(gap))
        if best_key is None or key < best_key:
            best, best_key = ev, key
    return best


# ------------------------------------------------------------------ stats
def index_players(box):
    idx = {}
    for team in box.get("players", []):
        for grp in team.get("statistics", []):
            gname = (grp.get("name") or "").lower()
            labels = grp.get("labels") or []
            for ath in grp.get("athletes", []):
                name = (ath.get("athlete") or {}).get("displayName")
                if not name:
                    continue
                entry = idx.setdefault(name, {"groups": [], "played": False})
                stats = ath.get("stats") or []
                if stats and not ath.get("didNotPlay"):
                    entry["played"] = True
                    entry["groups"].append((gname, labels, stats))
    return idx


def parse_value(label, raw):
    if raw in (None, "", "--", "-"):
        return None  # missing data is NOT zero
    s = str(raw).strip()
    if label in ("3PT", "C/ATT") and re.search(r"\d[-/]\d", s):
        parts = re.split(r"[-/]", s)
        return safe_float(parts[0] if label == "3PT" else parts[1])
    return safe_float(s)


def find_component(groups, table, labels):
    """-> ('ok', value) | ('no_table', None) | ('no_label', None)"""
    table_seen = False
    for gname, labs, stats in groups:
        if table and gname != table:
            continue
        table_seen = True
        for lab in labels:
            if lab in labs:
                i = labs.index(lab)
                if i < len(stats):
                    val = parse_value(lab, stats[i])
                    if val is not None:
                        return "ok", val
    return ("no_label" if table_seen else "no_table"), None


def box_has_data(idx, recipes):
    """True if at least one player in the game has a nonzero value for every
    component of some recipe. Catches box scores where a stat column exists
    but is blank/zero for everyone (e.g. NHL shots all showing 0)."""
    for recipe in recipes:
        ok = True
        for table, labels in recipe:
            found = False
            for entry in idx.values():
                status, val = find_component(entry["groups"], table, labels)
                if status == "ok" and val:
                    found = True
                    break
            if not found:
                ok = False
                break
        if ok:
            return True
    return False


def stat_total(entry, recipes, is_football):
    for recipe in recipes:
        total, ok = 0.0, True
        for table, labels in recipe:
            status, val = find_component(entry["groups"], table, labels)
            if status == "no_table" and is_football and table:
                val = 0.0   # e.g. QB with no rushing attempts isn't in the rushing table
            elif status != "ok":
                ok = False
                break
            total += val
        if ok:
            return total
    return None


# ------------------------------------------------------------------ grading
def grade_row(row):
    """-> ('STAT', value, note) | ('VOID', None, reason) | None (not ready)"""
    market = row["Market"]
    leagues = [ESPN_LEAGUES[row["Sport"]]] if row["Sport"] in ESPN_LEAGUES \
        else MARKET_FALLBACK_LEAGUES.get(market, [])
    commence = parse_iso(row["Commence Time"])
    anchor = commence or parse_iso(row["Timestamp"])
    if anchor is None:
        return None

    d = anchor.astimezone(ET).date()
    offsets = (-1, 0, 1) if commence else (0, 1)
    dates = [(d + timedelta(days=o)).strftime("%Y%m%d") for o in offsets]

    for sport, league in leagues:
        recipes = STAT_MAP[sport].get(market)
        if not recipes:
            continue
        events = [ev for ymd in dates for ev in scoreboard(sport, league, ymd)]
        ev = find_game(row["Game"], events, anchor, exact_start=commence is not None)
        if ev is None:
            if events and datetime.now(timezone.utc) - anchor > timedelta(hours=6):
                print(f"  ! no ESPN game matched '{row['Game']}' ({row['Player']}) — check team names")
            continue
        status = ev.get("status", {}).get("type", {})
        if status.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED"):
            return "VOID", None, "postponed"
        if not status.get("completed"):
            return None
        box = boxscore(sport, league, ev["id"])
        if not box:
            return None
        idx = index_players(box)
        name = match_name(row["Player"], list(idx))
        if name is None:
            return "VOID", None, "not in box score"
        if not idx[name]["played"]:
            return "VOID", None, "DNP"
        if sport != "football" and not box_has_data(idx, recipes):
            labels = sorted({l for e in idx.values() for _, labs, _ in e["groups"] for l in labs})
            print(f"  ! {market} is blank/zero for every player in {row['Game']} — "
                  f"treating as missing data. Box labels: {labels}")
            return "VOID", None, "stat missing from box score"
        total = stat_total(idx[name], recipes, sport == "football")
        if total is None:
            print(f"  ! stat labels not found for {row['Player']} {market} — check ESPN labels")
            return None
        return "STAT", total, ""
    return None


# ------------------------------------------------------------------ reporting
TIERS = [(0.0, 3.5, "<3.5%"), (3.5, 5.0, "3.5–5%"), (5.0, float("inf"), "5%+")]
def _diff(a, b):
    a, b = safe_float(a), safe_float(b)
    return None if a is None or b is None else a - b


# Line movement = close CLV minus entry edge, measured with the same bet price.
# Positive = that fair line moved TOWARD your bet after you placed it.
# Unlike CLV, it doesn't automatically reward the method whose own edge picked the play.
METRICS = {
    "CLV FD %": lambda r: safe_float(r.get("CLV FD %")),
    "CLV %": lambda r: safe_float(r.get("CLV %")),
    "MOVE FD": lambda r: _diff(r.get("CLV FD %"), r.get("FD Edge %")),
    "MOVE CONS": lambda r: _diff(r.get("CLV %"), r.get("Cons Edge %")),
}
METRIC_LABELS = {
    "CLV FD %": ("CLV(FD close)", "beat"),
    "CLV %": ("CLV(cons close)", "beat"),
    "MOVE FD": ("Move(FD)", "toward"),
    "MOVE CONS": ("Move(cons)", "toward"),
}


def tier_of(row, field="Edge %"):
    e = safe_float(row.get(field), None)
    if e is None:
        e = safe_float(row["Edge %"], 0.0)
    for i, (lo, hi, _) in enumerate(TIERS):
        if lo <= e < hi:
            return i
    return 0


def new_bucket():
    return {"W": 0, "L": 0, "P": 0, "V": 0, "units": 0.0, "staked": 0.0,
            "clv": {f: [0.0, 0, 0] for f in METRICS}}


def add(b, row):
    res = row["Result"]
    b[{"WIN": "W", "LOSS": "L", "PUSH": "P", "VOID": "V"}[res]] += 1
    if res in ("WIN", "LOSS"):
        b["staked"] += safe_float(row["Kelly Units"], 0.0)
    b["units"] += safe_float(row["Net Units"], 0.0)
    if res != "VOID":
        for f, getter in METRICS.items():
            v = getter(row)
            if v is not None:
                c = b["clv"][f]
                c[0] += v
                c[1] += 1
                c[2] += v > 0


def fmt(b, clv=("CLV FD %", "CLV %")):
    decided = b["W"] + b["L"]
    win = f" ({b['W'] / decided * 100:.1f}%)" if decided else ""
    roi = b["units"] / b["staked"] * 100 if b["staked"] else 0.0
    s = f"{b['W']}-{b['L']}-{b['P']}{win} | {b['units']:+.2f}u ({roi:+.1f}%)"
    for f in clv:
        tot, n, pos = b["clv"][f]
        if n:
            label, word = METRIC_LABELS[f]
            s += f" | {label} {tot / n:+.1f}% ({word} {pos / n * 100:.0f}%, n={n})"
    return s


def group_of(row):
    if not row["Event ID"]:
        return "legacy"          # original FanDuel-only scanner
    if not row["Method"]:
        return "pretest"         # consensus scanner before the side-by-side test
    return "test"


# ---------- compact table helpers (monospace code blocks line up in Discord)
def _rec(b):
    return f"{b['W']}-{b['L']}" + (f"-{b['P']}" if b["P"] else "")


def _units(b):
    return f"{b['units']:+.2f}"


def _roi(b):
    roi = round((b["units"] / b["staked"] * 100) if b["staked"] else 0)
    return f"{roi:+d}%" if roi else "0%"


def _avg(b, f):
    tot, n, _ = b["clv"][f]
    return f"{tot / n:+.1f}" if n else "—"


def _toward(b, f):
    _, n, pos = b["clv"][f]
    return f"{pos / n * 100:.0f}%" if n else "—"


def _table(header, rows, widths):
    def line(cells):
        first = f"{cells[0]:<{widths[0]}}"
        return first + "".join(f"{c:>{w}}" for c, w in zip(cells[1:], widths[1:]))
    return "```\n" + "\n".join([line(header)] + [line(r) for r in rows]) + "\n```"


EDGE_BUCKETS = [
    (0.0, 3.5, "2–3.5%"),
    (3.5, 4.5, "3.5–4.5%"),
    (4.5, 6.0, "4.5–6%"),
    (6.0, 8.0, "6–8%"),
    (8.0, float("inf"), "8%+"),
]


def _sum(buckets):
    out = new_bucket()
    for b in buckets:
        for k in ("W", "L", "P", "V", "units", "staked"):
            out[k] += b[k]
    return out


def _same(a, b):
    return all(a[k] == b[k] for k in ("W", "L", "P", "V")) and abs(a["units"] - b["units"]) < 0.005


def _bucket(rows):
    b = new_bucket()
    for r in rows:
        add(b, r)
    return b


UNDERS_OVERLAPS = [
    ("+ Candidate", meets_candidate),   # plain-text labels: emoji break column alignment in code blocks
    ("+ Rule 3", meets_rule3),
    ("+ Rule 5", meets_rule5),
]


def _unders_overlap_table(rows):
    """Unders plays split by which other tracked rules they also meet. A play can be in several
    '+' rows (e.g. 🧪 and 🧫), so those rows overlap; 'Unders only' and 'Any overlap' add up to the total."""
    only = [r for r in rows if not any(f(r) for _, f in UNDERS_OVERLAPS)]
    anyo = [r for r in rows if any(f(r) for _, f in UNDERS_OVERLAPS)]
    table = []
    for label, sub in [("All Unders", rows), ("Unders only", only), ("Any overlap", anyo)]:
        b = _bucket(sub)
        table.append([label, _rec(b), _units(b), _roi(b)])
    for label, f in UNDERS_OVERLAPS:
        b = _bucket([r for r in rows if f(r)])
        table.append([f"  {label}", _rec(b), _units(b), _roi(b)])
    return _table(["", "Rec", "Units", "ROI"], table, [13, 8, 7, 6])


def build_report(batch, all_rows):
    """Returns {"fields": [...embed fields...], "text": plain-text version for the log}.
    Every number comes from the same graded rows; a reconciliation check at the end
    confirms each table adds back up to its totals."""
    graded = [r for r in all_rows if r["Result"] in GRADED]
    by_group = {"legacy": [], "pretest": [], "test": []}
    for r in graded:
        by_group[group_of(r)].append(r)
    test, pre, old = by_group["test"], by_group["pretest"], by_group["legacy"]
    test_total, pre_total, old_total = _bucket(test), _bucket(pre), _bucket(old)
    all_total = _bucket(graded)
    checks = []   # (description, passed)
    fields = []

    overlap_defs = (
        ("FD disagr", lambda r: r["Method"] == "fd" and r["Cons Edge %"] != ""),
        ("FD SOLO", lambda r: r["Method"] == "fd" and r["Cons Edge %"] == ""),
        ("Cons only", lambda r: r["Method"] == "consensus"),
        ("Both", lambda r: r["Method"] == "both"),
    )
    overlap = {label: _bucket([r for r in test if match(r)]) for label, match in overlap_defs}

    if not test:
        fields.append({"name": "🧪 Side-by-side test", "value": "No graded test plays yet."})
    else:
        # 1) Method totals + edge tiers + test total
        rows, method_totals = [], {}
        for label, members, edge_field in (
            ("FD devig", ("fd", "both"), "FD Edge %"),
            ("Consensus", ("consensus", "both"), "Cons Edge %"),
        ):
            total = new_bucket()
            tiers = [new_bucket() for _ in TIERS]
            for r in test:
                if r["Method"] in members:
                    add(total, r)
                    add(tiers[tier_of(r, edge_field)], r)
            method_totals[label] = total
            rows.append([label, _rec(total), _units(total), _roi(total),
                         _avg(total, "MOVE FD"), _toward(total, "MOVE FD")])
            for i, (_, _, tl) in enumerate(TIERS):
                t = tiers[i]
                if t["W"] + t["L"] + t["P"]:
                    rows.append([f" {tl}", _rec(t), _units(t), _roi(t),
                                 _avg(t, "MOVE FD"), _toward(t, "MOVE FD")])
            checks.append((f"{label} tiers = {label} total", _same(_sum(tiers), total)))
        rows.append(["Test total", _rec(test_total), _units(test_total), _roi(test_total),
                     _avg(test_total, "MOVE FD"), _toward(test_total, "MOVE FD")])
        fields.append({"name": "🧪 FanDuel devig vs Consensus",
                       "value": _table(["", "Rec", "Units", "ROI", "Move", "Tw"], rows, [10, 7, 7, 6, 6, 5])})
        checks.append(("FD devig = FD disagr + SOLO + Both",
                       _same(method_totals["FD devig"], _sum([overlap["FD disagr"], overlap["FD SOLO"], overlap["Both"]]))))
        checks.append(("Consensus = Cons only + Both",
                       _same(method_totals["Consensus"], _sum([overlap["Cons only"], overlap["Both"]]))))

        # 2) Where the methods differ
        rows = [[label, _rec(b), _units(b), _avg(b, "MOVE FD"), _avg(b, "MOVE CONS"), _toward(b, "MOVE FD")]
                for label, b in overlap.items()]
        fields.append({"name": "🔍 Where they differ (adds up to Test total)",
                       "value": _table(["", "Rec", "Units", "MvFD", "MvCn", "Tw"], rows, [10, 7, 7, 6, 6, 5])})
        checks.append(("Overlap groups = Test total", _same(_sum(overlap.values()), test_total)))

        # 3) Books
        books = defaultdict(new_bucket)
        for r in test:
            add(books[r["Bookmaker"]], r)
        rows = [[book[:12], _rec(b), _units(b), _roi(b), _avg(b, "MOVE FD")]
                for book, b in sorted(books.items(), key=lambda kv: -kv[1]["units"])]
        fields.append({"name": "📚 Books (test)",
                       "value": _table(["", "Rec", "Units", "ROI", "Move"], rows, [12, 7, 7, 6, 6])})
        checks.append(("Books = Test total", _same(_sum(books.values()), test_total)))

    # 4) ROI by edge bucket — every play from the current scanner (pre-test + test)
    current = pre + test
    edge_compact = None
    if current:
        buckets = [new_bucket() for _ in EDGE_BUCKETS]
        for r in current:
            e = safe_float(r["Edge %"], 0.0)
            for i, (lo, hi, _) in enumerate(EDGE_BUCKETS):
                if lo <= e < hi:
                    add(buckets[i], r)
                    break
        rows = [[label, _rec(b), _units(b), _roi(b), _avg(b, "MOVE FD")]
                for (_, _, label), b in zip(EDGE_BUCKETS, buckets) if b["W"] + b["L"] + b["P"] + b["V"]]
        edge_compact = _table(["Edge", "Rec", "Units", "ROI"], [r[:4] for r in rows], [10, 7, 7, 6])
        fields.append({"name": "📈 ROI by edge (pre-test + test)",
                       "value": _table(["Edge", "Rec", "Units", "ROI", "Move"], rows, [10, 7, 7, 6, 6])})
        checks.append(("Edge buckets = Pre-test + Test", _same(_sum(buckets), _sum([pre_total, test_total]))))

    # 5) My rule — only plays scanned on/after RULE_START (judged on new data)
    rule_rows = [r for r in graded if meets_rule(r) and (r["Timestamp"] or "")[:10] >= RULE_START]
    if rule_rows:
        days = defaultdict(list)
        for r in rule_rows:
            days[r["Timestamp"][:10]].append(r)
        total = _bucket(rule_rows)
        table = [["Total", _rec(total), _units(total), _roi(total), _avg(total, "MOVE FD"), _toward(total, "MOVE FD")]]
        for day in sorted(days)[-5:]:
            b = _bucket(days[day])
            table.append([day[5:], _rec(b), _units(b), _roi(b), _avg(b, "MOVE FD"), _toward(b, "MOVE FD")])
        value = _table(["", "Rec", "Units", "ROI", "Move", "Tw"], table, [10, 7, 7, 6, 6, 5])
    else:
        value = "No graded rule plays yet."
    fields.append({"name": f"⭐ My rule: FD-method, edge ≥{RULE_MIN_EDGE}% (since {RULE_START[5:]})",
                   "value": value})

    # 5b) Candidate rule — tracked from CANDIDATE_START, same layout as the rule table
    cand_rows = [r for r in graded if meets_candidate(r) and (r["Timestamp"] or "")[:10] >= CANDIDATE_START]
    cand_name = (f"🧪 Candidate: FD disagreed, edge ≥{CANDIDATE_MIN_EDGE:g}%, cons ≥{CANDIDATE_MIN_CONS:g}% "
                 f"(since {CANDIDATE_START[5:]})")
    if cand_rows:
        cdays = defaultdict(list)
        for r in cand_rows:
            cdays[r["Timestamp"][:10]].append(r)
        ctotal = _bucket(cand_rows)
        ctable = [["Total", _rec(ctotal), _units(ctotal), _roi(ctotal), _avg(ctotal, "MOVE FD"), _toward(ctotal, "MOVE FD")]]
        for cday in sorted(cdays)[-5:]:
            b = _bucket(cdays[cday])
            ctable.append([cday[5:], _rec(b), _units(b), _roi(b), _avg(b, "MOVE FD"), _toward(b, "MOVE FD")])
        cvalue = _table(["", "Rec", "Units", "ROI", "Move", "Tw"], ctable, [10, 7, 7, 6, 6, 5])
    else:
        cvalue = "No graded candidate plays yet."
    fields.append({"name": cand_name, "value": cvalue})

    # 5c) Rule 3 — live since R3_START, plus a backtest from R3_BACKTEST_FROM
    r3_all = [r for r in graded if meets_rule3(r) and (r["Timestamp"] or "")[:10] >= R3_BACKTEST_FROM]
    r3_live = [r for r in r3_all if r["Timestamp"][:10] >= R3_START]
    r3_label = f"FD only, edge {R3_MIN_EDGE:g}-{R3_MAX_EDGE:g}%, odds {R3_MIN_ODDS:+d} to {R3_MAX_ODDS:+d}"
    if r3_all:
        r3days = defaultdict(list)
        for r in r3_live:
            r3days[r["Timestamp"][:10]].append(r)
        b_live, b_all = _bucket(r3_live), _bucket(r3_all)
        r3table = [[f"Since {R3_START[5:]}", _rec(b_live), _units(b_live), _roi(b_live),
                    _avg(b_live, "MOVE FD"), _toward(b_live, "MOVE FD")]]
        for r3day in sorted(r3days)[-5:]:
            b = _bucket(r3days[r3day])
            r3table.append([r3day[5:], _rec(b), _units(b), _roi(b), _avg(b, "MOVE FD"), _toward(b, "MOVE FD")])
        r3table.append([f"Back {R3_BACKTEST_FROM[5:]}+", _rec(b_all), _units(b_all), _roi(b_all),
                        _avg(b_all, "MOVE FD"), _toward(b_all, "MOVE FD")])
        r3value = _table(["", "Rec", "Units", "ROI", "Move", "Tw"], r3table, [10, 7, 7, 6, 6, 5])
    else:
        r3value = "No graded rule-3 plays yet."
    fields.append({"name": f"🧫 Rule 3: {r3_label}", "value": r3value})

    # 5d) Moderate dogs — full history from the log, split by edge band
    dog_rows = [r for r in graded if meets_dogs(r)]
    bands = " or ".join(f"{lo:g}-{hi:g}%" for lo, hi in DOGS_EDGE_BANDS)
    dog_label = f"odds {DOGS_MIN_ODDS:+d} to {DOGS_MAX_ODDS:+d}, edge {bands}"
    if dog_rows:
        b_dog = _bucket(dog_rows)
        dtable = [["History", _rec(b_dog), _units(b_dog), _roi(b_dog), _avg(b_dog, "MOVE FD"), _toward(b_dog, "MOVE FD")]]
        for lo, hi in DOGS_EDGE_BANDS:
            b = _bucket([r for r in dog_rows if lo <= safe_float(r["Edge %"], 0.0) < hi])
            dtable.append([f"  {lo:g}-{hi:g}%", _rec(b), _units(b), _roi(b), _avg(b, "MOVE FD"), _toward(b, "MOVE FD")])
        dvalue = _table(["", "Rec", "Units", "ROI", "Move", "Tw"], dtable, [10, 7, 7, 6, 6, 5])
    else:
        dvalue = "No graded moderate-dog plays yet."
    fields.append({"name": f"🐶 Moderate dogs: {dog_label}", "value": dvalue})

    # 5e) Rule 5 — live since R5_START, plus a backtest from R5_BACKTEST_FROM
    r5_all = [r for r in graded if meets_rule5(r) and (r["Timestamp"] or "")[:10] >= R5_BACKTEST_FROM]
    r5_live = [r for r in r5_all if r["Timestamp"][:10] >= R5_START]
    r5_label = f"{R5_BOOK}, FD only, odds {R5_MIN_ODDS:+d} to {R5_MAX_ODDS:+d}"
    if r5_all:
        r5days = defaultdict(list)
        for r in r5_live:
            r5days[r["Timestamp"][:10]].append(r)
        b5_live, b5_all = _bucket(r5_live), _bucket(r5_all)
        r5table = [[f"Since {R5_START[5:]}", _rec(b5_live), _units(b5_live), _roi(b5_live),
                    _avg(b5_live, "MOVE FD"), _toward(b5_live, "MOVE FD")]]
        for r5day in sorted(r5days)[-5:]:
            b = _bucket(r5days[r5day])
            r5table.append([r5day[5:], _rec(b), _units(b), _roi(b), _avg(b, "MOVE FD"), _toward(b, "MOVE FD")])
        r5table.append([f"Back {R5_BACKTEST_FROM[5:]}+", _rec(b5_all), _units(b5_all), _roi(b5_all),
                        _avg(b5_all, "MOVE FD"), _toward(b5_all, "MOVE FD")])
        r5value = _table(["", "Rec", "Units", "ROI", "Move", "Tw"], r5table, [10, 7, 7, 6, 6, 5])
    else:
        r5value = "No graded rule-5 plays yet."
    fields.append({"name": f"😈 Rule 5: {r5_label}", "value": r5value})

    # 5g) Hope — live since HOPE_START, plus a backtest from HOPE_BACKTEST_FROM
    hope_all = [r for r in graded if meets_hope(r) and (r["Timestamp"] or "")[:10] >= HOPE_BACKTEST_FROM]
    hope_live = [r for r in hope_all if r["Timestamp"][:10] >= HOPE_START]
    hope_label = (f"Hope (all Unders + NFL Rec/Rush Yds Overs, no NFL Rec Yds, "
                  f"edge {HOPE_MIN_EDGE:g}-{HOPE_MAX_EDGE:g}%)")
    if hope_all:
        hdays = defaultdict(list)
        for r in hope_live:
            hdays[r["Timestamp"][:10]].append(r)
        bh_live, bh_all = _bucket(hope_live), _bucket(hope_all)
        htable = [[f"Since {HOPE_START[5:]}", _rec(bh_live), _units(bh_live), _roi(bh_live),
                   _avg(bh_live, "MOVE FD"), _toward(bh_live, "MOVE FD")]]
        for hday in sorted(hdays)[-5:]:
            b = _bucket(hdays[hday])
            htable.append([hday[5:], _rec(b), _units(b), _roi(b), _avg(b, "MOVE FD"), _toward(b, "MOVE FD")])
        htable.append([f"Back {HOPE_BACKTEST_FROM[5:]}+", _rec(bh_all), _units(bh_all), _roi(bh_all),
                       _avg(bh_all, "MOVE FD"), _toward(bh_all, "MOVE FD")])
        hvalue = _table(["", "Rec", "Units", "ROI", "Move", "Tw"], htable, [12, 8, 7, 6, 6, 5])
    else:
        hvalue = "No graded Hope plays yet."
    fields.append({"name": f"👻 {hope_label}", "value": hvalue})

    # 5f) Unders — live since UNDERS_START, plus a backtest from UNDERS_BACKTEST_FROM
    un_all = [r for r in graded if meets_unders(r) and (r["Timestamp"] or "")[:10] >= UNDERS_BACKTEST_FROM]
    un_live = [r for r in un_all if r["Timestamp"][:10] >= UNDERS_START]
    un_label = (f"Unders (FD disagreed, edge {UNDERS_MIN_EDGE:g}-<{UNDERS_MAX_EDGE:g}%, "
                f"odds {UNDERS_MIN_ODDS:+d} to {UNDERS_MAX_ODDS:+d}, no Ast/Saves/3s)")
    if un_all:
        undays = defaultdict(list)
        for r in un_live:
            undays[r["Timestamp"][:10]].append(r)
        bu_live, bu_all = _bucket(un_live), _bucket(un_all)
        untable = [[f"Since {UNDERS_START[5:]}", _rec(bu_live), _units(bu_live), _roi(bu_live),
                    _avg(bu_live, "MOVE FD"), _toward(bu_live, "MOVE FD")]]
        for unday in sorted(undays)[-5:]:
            b = _bucket(undays[unday])
            untable.append([unday[5:], _rec(b), _units(b), _roi(b), _avg(b, "MOVE FD"), _toward(b, "MOVE FD")])
        untable.append([f"Back {UNDERS_BACKTEST_FROM[5:]}+", _rec(bu_all), _units(bu_all), _roi(bu_all),
                        _avg(bu_all, "MOVE FD"), _toward(bu_all, "MOVE FD")])
        unvalue = _table(["", "Rec", "Units", "ROI", "Move", "Tw"], untable, [12, 8, 7, 6, 6, 5])
    else:
        unvalue = "No graded Unders plays yet."
    fields.append({"name": f"🏴‍☠️ {un_label}", "value": unvalue})
    if un_all:
        fields.append({"name": f"🏴‍☠️ Unders overlap — since {UNDERS_START[5:]}",
                       "value": _unders_overlap_table(un_live)})
        fields.append({"name": f"🏴‍☠️ Unders overlap — back {UNDERS_BACKTEST_FROM[5:]}+",
                       "value": _unders_overlap_table(un_all)})

    # 6) Totals
    batch_total = _bucket(batch)
    checks.append(("All-time = Legacy + Pre-test + Test", _same(all_total, _sum([old_total, pre_total, test_total]))))
    checks.append(("Every graded play is in exactly one group", len(old) + len(pre) + len(test) == len(graded)))

    # Day record: every play scanned on the betting day, built up across grader runs.
    # The day is the most recent scan date among plays settled this run
    # (or the most recent graded day for a report-only run).
    dated = [r for r in (batch or graded) if (r["Timestamp"] or "")[:10]]
    day = max(r["Timestamp"][:10] for r in dated) if dated else None
    day_lines = []
    if day:
        day_rows = [r for r in all_rows if (r["Timestamp"] or "")[:10] == day]
        day_total = _bucket([r for r in day_rows if r["Result"] in GRADED])
        day_pending = sum(r["Result"] == "PENDING" for r in day_rows)
        day_rule = _bucket([r for r in day_rows if r["Result"] in GRADED and meets_rule(r)])
        label = datetime.strptime(day, "%Y-%m-%d").strftime("%a %b %-d")
        pend = f" • {day_pending} pending" if day_pending else " • all settled"
        day_lines.append(f"📅 **{label}:** {_rec(day_total)} | {_units(day_total)}u "
                         f"(${day_total['units'] * UNIT_SIZE:+.2f}){pend}")
        if day_rule["W"] + day_rule["L"] + day_rule["P"]:
            day_lines.append(f"↳ ⭐ Rule plays: {_rec(day_rule)} | {_units(day_rule)}u "
                             f"(${day_rule['units'] * UNIT_SIZE:+.2f})")

    totals = day_lines + [
        f"🔄 This run: {_rec(batch_total)} | {_units(batch_total)}u newly settled",
        f"🧪 Test: {_rec(test_total)} | {_units(test_total)}u ({_roi(test_total)})",
        f"🆕 Pre-test: {_rec(pre_total)} | {_units(pre_total)}u ({_roi(pre_total)}) | CLV {_avg(pre_total, 'CLV %')}%",
        f"📜 Legacy FD: {_rec(old_total)} | {_units(old_total)}u ({_roi(old_total)})",
        f"🏦 All-time: {_rec(all_total)} | {_units(all_total)}u ({_roi(all_total)})",
        f"⚪ Voids: {all_total['V']} lifetime ({batch_total['V']} this run)",
    ]
    failed = [desc for desc, ok in checks if not ok]
    if failed:
        totals.append("⚠️ **Reconciliation mismatch:** " + "; ".join(failed))
    else:
        totals.append(f"✅ All {len(checks)} reconciliation checks passed")
    fields.append({"name": "📦 Totals", "value": "\n".join(totals)})

    text = "\n\n".join(f"{f['name']}\n{f['value']}" for f in fields)

    # ---------- compact daily card (default)
    compact = []
    headline = "📊 EV Report"
    if day:
        headline = (f"📊 {label} — {_rec(day_total)} | {_units(day_total)}u "
                    f"(${day_total['units'] * UNIT_SIZE:+.2f}){pend}")
    rule_total = _bucket(rule_rows)
    rule_table = []
    if day and day >= RULE_START:
        rule_table.append(["Today", _rec(day_rule), _units(day_rule), _roi(day_rule)])
    rule_table.append([f"Since {RULE_START[5:]}", _rec(rule_total), _units(rule_total), _roi(rule_total)])
    compact.append({"name": f"⭐ My rule (FD-method, edge ≥{RULE_MIN_EDGE}%)",
                    "value": _table(["", "Rec", "Units", "ROI"], rule_table, [12, 7, 7, 6])})
    cand_total = _bucket(cand_rows)
    cand_table = []
    if day and day >= CANDIDATE_START:
        day_cand = _bucket([r for r in all_rows if (r["Timestamp"] or "")[:10] == day
                            and r["Result"] in GRADED and meets_candidate(r)])
        cand_table.append(["Today", _rec(day_cand), _units(day_cand), _roi(day_cand)])
    cand_table.append([f"Since {CANDIDATE_START[5:]}", _rec(cand_total), _units(cand_total), _roi(cand_total)])
    compact.append({"name": f"🧪 Candidate (FD disagreed, edge ≥{CANDIDATE_MIN_EDGE:g}%, cons ≥{CANDIDATE_MIN_CONS:g}%)",
                    "value": _table(["", "Rec", "Units", "ROI"], cand_table, [12, 7, 7, 6])})
    r3_table = []
    if day and day >= R3_START:
        day_r3 = _bucket([r for r in all_rows if (r["Timestamp"] or "")[:10] == day
                          and r["Result"] in GRADED and meets_rule3(r)])
        r3_table.append(["Today", _rec(day_r3), _units(day_r3), _roi(day_r3)])
    live_r3 = _bucket(r3_live)
    back_r3 = _bucket(r3_all)
    r3_table.append([f"Since {R3_START[5:]}", _rec(live_r3), _units(live_r3), _roi(live_r3)])
    r3_table.append([f"Back {R3_BACKTEST_FROM[5:]}+", _rec(back_r3), _units(back_r3), _roi(back_r3)])
    compact.append({"name": f"🧫 Rule 3 ({r3_label})",
                    "value": _table(["", "Rec", "Units", "ROI"], r3_table, [12, 7, 7, 6])})
    dog_table = []
    if day:
        day_dog = _bucket([r for r in all_rows if (r["Timestamp"] or "")[:10] == day
                           and r["Result"] in GRADED and meets_dogs(r)])
        dog_table.append(["Today", _rec(day_dog), _units(day_dog), _roi(day_dog)])
    hist_dog = _bucket(dog_rows)
    dog_table.append(["History", _rec(hist_dog), _units(hist_dog), _roi(hist_dog)])
    compact.append({"name": f"🐶 Moderate dogs ({dog_label})",
                    "value": _table(["", "Rec", "Units", "ROI"], dog_table, [12, 7, 7, 6])})
    r5_table = []
    if day and day >= R5_START:
        day_r5 = _bucket([r for r in all_rows if (r["Timestamp"] or "")[:10] == day
                          and r["Result"] in GRADED and meets_rule5(r)])
        r5_table.append(["Today", _rec(day_r5), _units(day_r5), _roi(day_r5)])
    live_r5, back_r5 = _bucket(r5_live), _bucket(r5_all)
    r5_table.append([f"Since {R5_START[5:]}", _rec(live_r5), _units(live_r5), _roi(live_r5)])
    r5_table.append([f"Back {R5_BACKTEST_FROM[5:]}+", _rec(back_r5), _units(back_r5), _roi(back_r5)])
    compact.append({"name": f"😈 Rule 5 ({r5_label})",
                    "value": _table(["", "Rec", "Units", "ROI"], r5_table, [12, 7, 7, 6])})
    hope_table = []
    if day and day >= HOPE_START:
        day_hope = _bucket([r for r in all_rows if (r["Timestamp"] or "")[:10] == day
                            and r["Result"] in GRADED and meets_hope(r)])
        hope_table.append(["Today", _rec(day_hope), _units(day_hope), _roi(day_hope)])
    live_hope, back_hope = _bucket(hope_live), _bucket(hope_all)
    hope_table.append([f"Since {HOPE_START[5:]}", _rec(live_hope), _units(live_hope), _roi(live_hope)])
    hope_table.append([f"Back {HOPE_BACKTEST_FROM[5:]}+", _rec(back_hope), _units(back_hope), _roi(back_hope)])
    compact.append({"name": f"👻 {hope_label}",
                    "value": _table(["", "Rec", "Units", "ROI"], hope_table, [12, 8, 7, 6])})
    un_table = []
    if day and day >= UNDERS_START:
        day_un = _bucket([r for r in all_rows if (r["Timestamp"] or "")[:10] == day
                          and r["Result"] in GRADED and meets_unders(r)])
        un_table.append(["Today", _rec(day_un), _units(day_un), _roi(day_un)])
    live_un, back_un = _bucket(un_live), _bucket(un_all)
    un_table.append([f"Since {UNDERS_START[5:]}", _rec(live_un), _units(live_un), _roi(live_un)])
    un_table.append([f"Back {UNDERS_BACKTEST_FROM[5:]}+", _rec(back_un), _units(back_un), _roi(back_un)])
    compact.append({"name": f"🏴‍☠️ {un_label}",
                    "value": _table(["", "Rec", "Units", "ROI"], un_table, [12, 7, 7, 6])})
    if un_live:
        compact.append({"name": f"🏴‍☠️ Unders overlap (since {UNDERS_START[5:]})",
                        "value": _unders_overlap_table(un_live)})
    if test:
        compact.append({"name": "🎯 Groups (test to date)",
                        "value": _table(["", "Rec", "Units", "ROI"],
                                        [[label_, _rec(b), _units(b), _roi(b)] for label_, b in overlap.items()],
                                        [12, 7, 7, 6])})
    if edge_compact:
        compact.append({"name": "📈 ROI by edge", "value": edge_compact})
    footer = [f"🏦 All-time: {_rec(all_total)} | {_units(all_total)}u ({_roi(all_total)})"]
    footer.append("⚠️ **Totals mismatch:** " + "; ".join(failed) if failed else "✅ Totals verified")
    compact.append({"name": "\u200b", "value": "\n".join(footer)})

    return {"fields": fields, "compact": compact, "headline": headline, "text": text}


LEGEND = ("Move = how far FanDuel's fair line shifted toward (+) or away (−) "
          "from the bet before game time  •  Tw = % of plays it moved toward  •  "
          "MvCn = same for the consensus line")


def send_report(report, n, regrade, title=None, full=False):
    print("\n" + report["text"])
    if not DISCORD_WEBHOOK_URL:
        return
    if full:
        title = title or f"📊 EV Auto-Grader ({n} settled{', FULL REGRADE' if regrade else ''})"
        chosen = report["fields"]
    else:
        title = report["headline"] + (" • FULL REGRADE" if regrade else "")
        chosen = report["compact"]
    embed = {"title": title, "color": 3447003,
             "fields": [{"name": f["name"], "value": f["value"][:1024], "inline": False} for f in chosen]}
    if full:
        embed["footer"] = {"text": LEGEND}
    try:
        SESSION.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
    except requests.RequestException:
        pass


# ------------------------------------------------------------------ main
def run():
    regrade = "--regrade" in sys.argv or os.environ.get("REGRADE") == "1"
    report_only = "--report-only" in sys.argv or os.environ.get("REPORT_ONLY") == "1"
    full = "--full" in sys.argv or os.environ.get("FULL_REPORT") == "1"
    rows = load_rows()
    if not rows:
        print("No plays to grade.")
        return

    if report_only and not regrade:
        # Post the current standings without grading or changing anything
        pending = sum(r["Result"] == "PENDING" for r in rows)
        send_report(build_report([], rows), 0, False,
                    title=f"📊 EV Report (no grading run • {pending} pending)", full=full)
        return

    if regrade:
        print("REGRADE: resetting all graded plays to PENDING")
        for r in rows:
            if r["Result"] in GRADED:
                r["Result"], r["Net Units"], r["Actual"] = "PENDING", "0.00", ""

    now = datetime.now(timezone.utc)
    batch = []
    for row in rows:
        if row["Result"] != "PENDING":
            continue
        anchor = parse_iso(row["Commence Time"]) or parse_iso(row["Timestamp"])
        if anchor and anchor > now:
            continue
        if not regrade and anchor and now - anchor > timedelta(days=MAX_AGE_DAYS):
            continue

        out = grade_row(row)
        if out is None:
            continue
        kind, val, note = out
        units = safe_float(row["Kelly Units"], 0.0)
        line = float(row["Line"])
        dec = american_to_decimal(parse_american(row["Odds"]))
        side = row["Side"].strip().lower()

        if kind == "VOID":
            res, net = "VOID", 0.0
            row["Actual"] = note
        else:
            row["Actual"] = f"{val:g}"
            if val == line:
                res, net = "PUSH", 0.0
            elif (side == "over" and val > line) or (side == "under" and val < line):
                res, net = "WIN", units * (dec - 1)
            else:
                res, net = "LOSS", -units
        row["Result"], row["Net Units"] = res, f"{net:.2f}"
        batch.append(row)
        print(f"Graded: {row['Player']} {side.upper()} {row['Line']} {row['Market']} -> {row['Actual']} ({res})")

    if not batch:
        print("No pending plays were ready to grade.")
        return
    save_rows(rows)
    send_report(build_report(batch, rows), len(batch), regrade, full=full)


if __name__ == "__main__":
    run()
