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
    ET, GRADED, UNIT_SIZE, american_to_decimal, load_rows, match_name,
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
        "Shots On Goal": [[(None, ("SOG", "S"))]],
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
TIERS = [(0.0, 3.5, "2–3.5% edge"), (3.5, 5.0, "3.5–5% edge"), (5.0, float("inf"), "5%+ edge")]


def tier_of(row):
    e = safe_float(row["Edge %"], 0.0)
    for i, (lo, hi, _) in enumerate(TIERS):
        if lo <= e < hi:
            return i
    return 0


def new_bucket():
    return {"W": 0, "L": 0, "P": 0, "V": 0, "units": 0.0, "staked": 0.0, "clv": 0.0, "clv_n": 0, "beat": 0}


def add(b, row):
    res = row["Result"]
    b[{"WIN": "W", "LOSS": "L", "PUSH": "P", "VOID": "V"}[res]] += 1
    units = safe_float(row["Kelly Units"], 0.0)
    if res in ("WIN", "LOSS"):
        b["staked"] += units
    b["units"] += safe_float(row["Net Units"], 0.0)
    clv = safe_float(row["CLV %"])
    if clv is not None and res != "VOID":
        b["clv"] += clv
        b["clv_n"] += 1
        b["beat"] += clv > 0


def fmt(b, show_clv=True):
    decided = b["W"] + b["L"]
    win = f" ({b['W'] / decided * 100:.1f}%)" if decided else ""
    roi = b["units"] / b["staked"] * 100 if b["staked"] else 0.0
    s = f"{b['W']}-{b['L']}-{b['P']}{win} | {b['units']:+.2f}u ({roi:+.1f}%)"
    if show_clv and b["clv_n"]:
        s += f" | CLV {b['clv'] / b['clv_n']:+.1f}% (beat {b['beat'] / b['clv_n'] * 100:.0f}%, n={b['clv_n']})"
    return s


def build_report(batch, all_rows):
    graded = [r for r in all_rows if r["Result"] in GRADED]
    tiers_b = [new_bucket() for _ in TIERS]
    tiers_l = [new_bucket() for _ in TIERS]
    sides, books = defaultdict(new_bucket), defaultdict(new_bucket)
    total_b, total_l = new_bucket(), new_bucket()

    for r in batch:
        add(tiers_b[tier_of(r)], r)
        add(total_b, r)
    for r in graded:
        add(tiers_l[tier_of(r)], r)
        add(total_l, r)
        add(sides[r["Side"].strip().title()], r)
        add(books[r["Bookmaker"]], r)

    lines = []
    for i, (_, _, label) in enumerate(TIERS):
        lines.append(f"**{label}**")
        lines.append(f"Batch: {fmt(tiers_b[i], show_clv=False)}")
        lines.append(f"Life: {fmt(tiers_l[i])}")
        lines.append("")
    lines.append("**Sides (lifetime)**")
    for side, icon in (("Over", "🔼"), ("Under", "🔽")):
        if side in sides:
            lines.append(f"{icon} {side}: {fmt(sides[side])}")
    lines.append("")
    lines.append("**Books (lifetime)**")
    for book, b in sorted(books.items(), key=lambda kv: -kv[1]["units"]):
        lines.append(f"• {book}: {fmt(b)}")
    lines.append("")
    lines.append(f"💰 **Batch:** {total_b['units']:+.2f}u (${total_b['units'] * UNIT_SIZE:+.2f})")
    lines.append(f"🏦 **Lifetime:** {fmt(total_l)} (${total_l['units'] * UNIT_SIZE:+.2f})")
    voids = total_b["V"]
    if voids:
        lines.append(f"⚪ {voids} voided this batch (DNP / postponed / not in box)")
    return "\n".join(lines)


def send_report(text, n, regrade):
    print("\n" + text)
    if not DISCORD_WEBHOOK_URL:
        return
    title = f"📊 EV Auto-Grader ({n} settled{', FULL REGRADE' if regrade else ''})"
    try:
        SESSION.post(DISCORD_WEBHOOK_URL, json={"embeds": [{
            "title": title, "description": text[:4000], "color": 3447003}]}, timeout=10)
    except requests.RequestException:
        pass


# ------------------------------------------------------------------ main
def run():
    regrade = "--regrade" in sys.argv or os.environ.get("REGRADE") == "1"
    rows = load_rows()
    if not rows:
        print("No plays to grade.")
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
    send_report(build_report(batch, rows), len(batch), regrade)


if __name__ == "__main__":
    run()
