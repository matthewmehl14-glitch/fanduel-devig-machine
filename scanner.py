"""
+EV prop scanner: weighted consensus devig (Novig + FanDuel anchored).

For every prop line, each book's two-way price is devigged (power method),
then blended into a weighted fair probability. Each book's price is compared
against the fair line built from the OTHER books, so a book never validates
its own price.

Also captures closing lines: every run re-snapshots pending plays for games
that haven't started, so the last pre-game snapshot becomes the "close" and
the grader can report CLV.
"""
import csv
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from common import (
    CT, UNIT_SIZE, american_to_decimal, american_to_prob, fmt_american,
    load_rows, match_name, STUDY_AGREE_EDGE, meets_gemini, meets_pj, meets_merica, meets_retail, CAL_MIN_EDGE, calibrated_prob_edge, fit_calibration, meets_lucky, meets_wtf, meets_calienta, meets_candidate, meets_chaching, meets_dogs, meets_rule, meets_hope, meets_rule3, meets_rule5, meets_unders, normalize_name, parse_american, parse_iso,
    play_key, prob_to_american, safe_float, save_rows,
)

API_KEY = os.environ.get("ODDS_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
BASE_URL = "https://api.the-odds-api.com/v4"

# ------------------------------------------------------------------ tuning
# Kansas books. Weight = how much each book's devigged line counts toward the
# fair price. Novig (exchange, near-zero vig) and FanDuel are the anchors.
BOOK_WEIGHTS = {
    "novig": 3.0,
    "fanduel": 2.5,
    "pinnacle": 3.0,        # reference only (added Nov 26; may not post props through the Odds API)
    "draftkings": 1.0,
    "betmgm": 0.75,
    "williamhill_us": 0.75, # Caesars. The Odds API key is "williamhill_us": "caesars" never returned prices
    "espnbet": 0.75,        # theScore Bet: reference only (account limited)
    "fanatics": 0.5,        # reference only (account limited)
}
# Books a play can be logged at. Reference-only books still shape the fair price, but a play is
# never logged at them; when one has the best price, the next-best bettable book gets the play.
BETTABLE_BOOKS = {"novig", "fanduel", "draftkings", "betmgm", "williamhill_us"}
SCANNER_VERSION = "2026-11-26"   # Caesars fixed, Pinnacle/Fanatics added, theScore/Fanatics reference-only
ANCHOR_BOOKS = {"novig", "fanduel"}

# Shadow "study" weights from the published player-prop study. Only books we already pull are
# used (Kalshi, ProphetX, Pinnacle and the small books aren't fetched), so this costs no credits.
# Tracking only: it is logged next to each play and never changes what is logged, staked or marked.
STUDY_WEIGHTS = {
    "novig": 7.0,
    "draftkings": 1.4,
    "fanduel": 1.0,
    "williamhill_us": 0.5,
    "betmgm": 0.0,      # "everything else off"
    "espnbet": 0.0,     # theScore Bet
}
STUDY_MIN_BOOKS = 2       # books with weight > 0 (excluding the one bet) needed for a study price
KS_BOOKS = ",".join(BOOK_WEIGHTS)

MIN_FAIR_BOOKS = 3        # two-way books (excluding target) needed for a fair line
MAX_ANCHOR_GAP = 0.04     # skip line if Novig and FanDuel disagree by > 4 pts of prob
REQUIRE_BOTH_ANCHORS = True   # Novig AND FanDuel must both post fresh two-way lines
ONE_PLAY_PER_PLAYER = True    # only the single best play per player per game
STALE_MINUTES = 10        # ignore quotes not refreshed within this window
MIN_EDGE = 0.035
MAX_EDGE = 0.15           # larger "edges" are almost always bad or stale data
MIN_DEC, MAX_DEC = 1.50, 3.00   # roughly -200 to +200; longshots are mostly noise

KELLY_FRACTION = 0.25
MAX_UNITS_PER_PLAY = 1.5
MAX_UNITS_PER_PLAYER_GAME = 2.0   # Points, PRA, P+R on one player = one correlated bet
MIN_UNITS = 0.10

# Credit control. Each game costs ~1 credit per prop market with odds posted,
# so only games starting soon are scanned. Hourly scans see every game ~3 times.
LOOKAHEAD_HOURS = 5          # only scan games starting within the next 5 hours
STEAM_DISCORD = False        # steam moves are still tracked (Last Seen Odds, line_history.csv) but not posted
STEAM_MIN_PP = 1.0           # steam alert when your book's price gets this many win-% points more expensive (−110 → −115 ≈ 1.1)
CLOSE_MIN_LEAD_MINUTES = 2   # closing-line snapshots stop 2 minutes before start
QUOTA_RESERVE = 200      # stop fetching odds if remaining credits drop below this

SPORTS_CONFIG = {
    "basketball_wnba": "player_points,player_rebounds,player_assists,player_points_rebounds,player_points_rebounds_assists,player_threes",
    "basketball_nba": "player_points,player_rebounds,player_assists,player_points_rebounds,player_points_rebounds_assists,player_threes",
    "basketball_nba_preseason": "player_points,player_rebounds,player_assists,player_points_rebounds,player_points_rebounds_assists,player_threes",
    "icehockey_nhl": "player_points,player_assists,player_shots_on_goal,player_total_saves",
    "icehockey_nhl_preseason": "player_points,player_assists,player_shots_on_goal,player_total_saves",
    "americanfootball_nfl": "player_pass_yds,player_pass_attempts,player_rush_yds,player_rush_attempts,player_reception_yds,player_receptions",
    "americanfootball_ncaaf": "player_pass_yds,player_pass_attempts,player_rush_yds,player_rush_attempts,player_reception_yds,player_receptions",
    "basketball_ncaab": "player_points",   # men's college basketball, points only (added Oct 31)
}

# Sports listed here are skipped entirely: no odds requests, no credits spent.
# Remove a sport from this set to turn it back on. Its markets above stay as they are.
PAUSED_SPORTS = {
    "americanfootball_ncaaf",
    "basketball_ncaab",          # paused Nov 2 to save credits; remove this line to turn it back on
}


# Sides never logged for these markets (Overs lost in both halves of the log; Unders were fine).
# Market names as they appear in the log. Remove an entry to start logging those Overs again.
SKIP_SIDES = {
    "Total Saves": {"Over"},
    "Assists": {"Over"},          # exact market only; "Points Rebounds Assists" Overs still log
    "Threes": {"Over"},
}


def format_market_name(market_key):
    return market_key.replace("player_", "").replace("_", " ").title()


DISPLAY_TO_KEY = {
    format_market_name(k): k
    for markets in SPORTS_CONFIG.values() for k in markets.split(",")
}

QUOTA = {"remaining": "?", "used": "?", "spent": 0}
SESSION = requests.Session()


# ------------------------------------------------------------------ credits
def low_quota():
    try:
        return float(QUOTA["remaining"]) < QUOTA_RESERVE
    except (TypeError, ValueError):
        return False


def credit_summary():
    """e.g. '💳 212 credits this scan • 9,980 left • ~713/day until Nov 1'"""
    try:
        left = int(float(QUOTA["remaining"]))
    except (TypeError, ValueError):
        return f"💳 {QUOTA['spent']} credits this scan"
    today = datetime.now(CT).date()
    reset = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    days = max((reset - today).days, 1)
    return (f"💳 {QUOTA['spent']} credits this scan • {left:,} left • "
            f"~{left // days:,}/day until {reset.strftime('%b')} {reset.day}")


# ------------------------------------------------------------------ http
def get_json(url, params):
    for attempt in range(3):
        try:
            res = SESSION.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            print(f"    network error ({e}), retry {attempt + 1}/3")
            time.sleep(2 * (attempt + 1))
            continue
        QUOTA["remaining"] = res.headers.get("x-requests-remaining", QUOTA["remaining"])
        QUOTA["used"] = res.headers.get("x-requests-used", QUOTA["used"])
        try:
            QUOTA["spent"] += int(float(res.headers.get("x-requests-last", 0)))
        except ValueError:
            pass
        if res.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        if res.status_code != 200:
            print(f"    API {res.status_code}: {res.text[:200]}")
            return None
        return res.json()
    return None


# ------------------------------------------------------------------ devig
def devig_power(p_over, p_under):
    """Power-method devig: find k with p_over^k + p_under^k = 1.
    Handles skewed lines better than proportional (doesn't overstate the
    longshot side). Exchange prices with no overround are just normalized."""
    total = p_over + p_under
    if total <= 1.0:
        return p_over / total, p_under / total
    lo, hi = 1.0, 20.0
    for _ in range(60):
        k = (lo + hi) / 2
        if p_over ** k + p_under ** k > 1:
            lo = k
        else:
            hi = k
    a, b = p_over ** lo, p_under ** lo
    return a / (a + b), b / (a + b)


def devigged(q, side):
    po, pu = devig_power(american_to_prob(q["Over"]), american_to_prob(q["Under"]))
    return po if side == "Over" else pu


def anchors_ok(quotes):
    """Both sharp anchors must post fresh two-way lines that agree.
    The target book counts as present, so Novig/FanDuel prices can still be bet."""
    probs = {}
    for bkey in ANCHOR_BOOKS:
        q = quotes.get(bkey)
        if q and q["fresh"] and "Over" in q and "Under" in q:
            probs[bkey] = devigged(q, "Over")
    if REQUIRE_BOTH_ANCHORS and len(probs) < len(ANCHOR_BOOKS):
        return False
    if len(probs) == 2 and abs(probs["novig"] - probs["fanduel"]) > MAX_ANCHOR_GAP:
        return False
    return True


def fair_prob(quotes, side, exclude=None):
    """Weighted devigged fair probability for `side`, excluding one book.
    Returns (prob, [book titles used]) or None if the line isn't trustworthy."""
    acc = total_w = 0.0
    used, anchors = [], {}
    for bkey, q in quotes.items():
        if bkey == exclude or not q["fresh"] or "Over" not in q or "Under" not in q:
            continue
        po, pu = devig_power(american_to_prob(q["Over"]), american_to_prob(q["Under"]))
        p = po if side == "Over" else pu
        w = BOOK_WEIGHTS.get(bkey, 0.5)
        acc += w * p
        total_w += w
        used.append(q["title"])
        if bkey in ANCHOR_BOOKS:
            anchors[bkey] = p
    if len(used) < MIN_FAIR_BOOKS or not anchors:
        return None
    if exclude is not None and len(anchors) == 2 and abs(anchors["novig"] - anchors["fanduel"]) > MAX_ANCHOR_GAP:
        return None  # the sharp books disagree, so nobody knows the true price
    return acc / total_w, used


def study_fair(quotes, side, exclude=None):
    """Fair probability under STUDY_WEIGHTS, excluding the book being bet.
    Returns (prob, [book titles used]) or None. Shadow only."""
    acc = total_w = 0.0
    used = []
    for bkey, q in quotes.items():
        w = STUDY_WEIGHTS.get(bkey, 0.0)
        if w <= 0 or bkey == exclude or not q["fresh"] or "Over" not in q or "Under" not in q:
            continue
        po, pu = devig_power(american_to_prob(q["Over"]), american_to_prob(q["Under"]))
        acc += w * (po if side == "Over" else pu)
        total_w += w
        used.append(q["title"])
    if len(used) < STUDY_MIN_BOOKS:
        return None
    return acc / total_w, used


def kelly_units(p, dec):
    b = dec - 1
    f = (p * b - (1 - p)) / b
    return max(0.0, f * 100 * KELLY_FRACTION)   # 1u = 1% of bankroll


# ------------------------------------------------------------------ parsing
def build_lines(event_data, now_utc):
    """-> {(market_key, canonical_player, point): {book_key: quote}}"""
    lines = {}
    canon = {}
    for book in event_data.get("bookmakers", []):
        bkey = book.get("key")
        if bkey not in BOOK_WEIGHTS:
            continue
        for market in book.get("markets", []):
            m_key = market.get("key")
            updated = parse_iso(market.get("last_update") or book.get("last_update"))
            fresh = updated is not None and (now_utc - updated) <= timedelta(minutes=STALE_MINUTES)
            names = canon.setdefault(m_key, [])
            for o in market.get("outcomes", []):
                pt, side, raw = o.get("point"), o.get("name"), o.get("description")
                if pt is None or side not in ("Over", "Under") or not raw:
                    continue
                player = match_name(raw, names)
                if player is None:
                    names.append(raw)
                    player = raw
                quote = lines.setdefault((m_key, player, float(pt)), {}).setdefault(
                    bkey, {"title": book.get("title", bkey), "fresh": fresh, "raw": raw}
                )
                quote[side] = o["price"]
                # betslip / market / event link, most specific first (only present with includeLinks=true)
                link = o.get("link") or market.get("link") or book.get("link")
                if link:
                    quote.setdefault("links", {})[side] = link
                sids = "|".join(f"{lvl}={x.get('sid')}" for lvl, x in (("event", book), ("market", market), ("sel", o))
                                if x.get("sid"))
                if sids:
                    quote.setdefault("sids", {})[side] = sids
    return lines


def fd_fair(quotes, side):
    """FanDuel-only devig (the original method)."""
    q = quotes.get("fanduel")
    if q and q["fresh"] and "Over" in q and "Under" in q:
        return devigged(q, side)
    return None


def find_edges(lines):
    """Evaluate every offer under BOTH methods and tag which one(s) qualify:
    'fd' = FanDuel-only devig, 'consensus' = weighted consensus, 'both'."""
    out = []
    for (m_key, player, pt), quotes in lines.items():
        cons_allowed = anchors_ok(quotes)
        skip = SKIP_SIDES.get(format_market_name(m_key), set())
        for bkey, q in quotes.items():
            if not q["fresh"] or bkey not in BETTABLE_BOOKS:
                continue
            for side in ("Over", "Under"):
                if side in skip:
                    continue  # filtered here, before the one-play-per-player pick, so Unders still log
                price = q.get(side)
                if price is None:
                    continue
                dec = american_to_decimal(price)
                if not (MIN_DEC <= dec <= MAX_DEC):
                    continue

                cons_p = cons_edge = None
                used = []
                if cons_allowed:
                    fp = fair_prob(quotes, side, exclude=bkey)
                    if fp:
                        cons_p, used = fp
                        cons_edge = cons_p * dec - 1

                fd_p = fd_edge = None
                if bkey != "fanduel":
                    fd_p = fd_fair(quotes, side)
                    if fd_p is not None:
                        fd_edge = fd_p * dec - 1

                cons_ok = cons_edge is not None and MIN_EDGE <= cons_edge <= MAX_EDGE
                fd_ok = fd_edge is not None and MIN_EDGE <= fd_edge <= MAX_EDGE
                if not (cons_ok or fd_ok):
                    continue
                if cons_ok and fd_ok:
                    method = "both"
                    # stake on the more conservative of the two estimates
                    p, edge = (cons_p, cons_edge) if cons_edge <= fd_edge else (fd_p, fd_edge)
                elif fd_ok:
                    method, p, edge = "fd", fd_p, fd_edge
                else:
                    method, p, edge = "consensus", cons_p, cons_edge

                st = study_fair(quotes, side, exclude=bkey)   # shadow only; doesn't affect selection
                out.append({
                    "study_p": st[0] if st else None, "study_used": st[1] if st else [],
                    "link": (q.get("links") or {}).get(side),
                    "sids": (q.get("sids") or {}).get(side) or "",
                    "m_key": m_key, "player": player, "raw": q["raw"], "pt": pt,
                    "side": side, "book": q["title"], "price": price, "dec": dec,
                    "p": p, "edge": edge, "used": used, "method": method,
                    "fd_p": fd_p, "fd_edge": fd_edge, "cons_p": cons_p, "cons_edge": cons_edge,
                })
    return out


STEAM_RULES = [("🍀", meets_lucky), ("⚡", meets_retail), ("🚒", meets_calienta), ("🏀", meets_pj), ("🇺🇸", meets_merica)]
STEAM_ALERTS = []   # filled during the scan, posted to Discord at the end


def _implied(price):
    return 1 / american_to_decimal(price)


def _steam_tags(row):
    return "".join(e for e, f in STEAM_RULES if f(row))


def _steam_price(row, cur_price, fair):
    """Price at your book moved against you since the last check -> queue an alert."""
    tags = _steam_tags(row)
    last = row.get("Last Seen Odds") or ""
    base = last if last and not last.startswith("line:") else row["Odds"]
    try:
        base_p = parse_american(base)
    except (TypeError, ValueError):
        base_p = None
    if base_p is None or cur_price is None:
        return
    moved = (_implied(cur_price) - _implied(base_p)) * 100
    if moved >= STEAM_MIN_PP:
        row["Last Seen Odds"] = fmt_american(cur_price)
        if tags:
            edge = (fair * american_to_decimal(cur_price) - 1) * 100 if fair else None
            STEAM_ALERTS.append({"row": row, "tags": tags, "kind": "price", "from": fmt_american(base_p),
                                 "to": fmt_american(cur_price), "edge": edge})
    elif moved < 0:
        row["Last Seen Odds"] = fmt_american(cur_price)   # got cheaper: reset the baseline


def _steam_line(row, lines, m_key, side):
    """Your line is gone and the same player now sits at a worse line -> queue an alert once per new line."""
    line = safe_float(row["Line"])
    all_players = {p for (mk, p, pt) in lines if mk == m_key}
    player = match_name(row["Player"], all_players)
    if player is None:
        return
    pts = sorted(pt for (mk, p, pt) in lines if mk == m_key and p == player)
    worse = [pt for pt in pts if (pt < line if side == "Under" else pt > line)]
    if not worse:
        return
    new = max(worse) if side == "Under" else min(worse)
    marker = f"line:{new:g}"
    if row.get("Last Seen Odds") == marker:
        return
    row["Last Seen Odds"] = marker
    tags = _steam_tags(row)
    if not tags:
        return
    q = next((q for q in lines[(m_key, player, new)].values() if q["title"] == row["Bookmaker"]), None)
    price = fmt_american(q[side]) if q and q.get(side) is not None else None
    STEAM_ALERTS.append({"row": row, "tags": tags, "kind": "line", "from": row["Line"], "to": f"{new:g}",
                         "price": price})


# ------------------------------------------------------------------ line history
HISTORY_FILE = "line_history.csv"
HISTORY_BOOKS = list(BOOK_WEIGHTS)          # every book the scanner pulls
HISTORY_BOOK_NAMES = {"novig": "Novig", "fanduel": "FanDuel", "pinnacle": "Pinnacle", "draftkings": "DraftKings",
                      "betmgm": "BetMGM", "williamhill_us": "Caesars", "espnbet": "theScore", "fanatics": "Fanatics"}
HISTORY_FIELDS = (["Scan Time", "Mins To Start", "Status", "Rules", "Logged At", "Event ID", "Game", "Sport",
                   "Player", "Market", "Side", "Line", "Book", "Logged Odds", "Logged Edge %",
                   "Book Now", "Cons Fair %", "FD Fair %", "Edge Now %", "Seen Line"]
                  + [f"{HISTORY_BOOK_NAMES.get(b, b)} Price" for b in HISTORY_BOOKS])
HISTORY = []   # rows appended during the scan, written once at the end


def _history_row(row, status, quotes=None, side=None, cons_p=None, fd_p=None, seen_line=""):
    """One snapshot of a pending rule play: your book's price, every book's price, and the fair prices."""
    tags = _steam_tags(row)
    if not tags:
        return
    now = datetime.now(timezone.utc)
    start = parse_iso(row.get("Commence Time"))
    h = {"Scan Time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "Mins To Start": f"{(start - now).total_seconds() / 60:.0f}" if start else "",
         "Status": status, "Rules": tags, "Logged At": row["Timestamp"], "Event ID": row["Event ID"],
         "Game": row["Game"], "Sport": row["Sport"], "Player": row["Player"], "Market": row["Market"],
         "Side": row["Side"], "Line": row["Line"], "Book": row["Bookmaker"], "Logged Odds": row["Odds"],
         "Logged Edge %": row["Edge %"], "Seen Line": seen_line,
         "Cons Fair %": f"{cons_p * 100:.2f}" if cons_p else "", "FD Fair %": f"{fd_p * 100:.2f}" if fd_p else ""}
    if quotes and side:
        for bkey in HISTORY_BOOKS:
            q = quotes.get(bkey)
            h[f"{HISTORY_BOOK_NAMES.get(bkey, bkey)} Price"] = fmt_american(q[side]) if q and q.get(side) is not None and q.get("fresh") else ""
        mine = next((q for q in quotes.values() if q["title"] == row["Bookmaker"]), None)
        if mine and mine.get(side) is not None:
            h["Book Now"] = fmt_american(mine[side])
            fair = cons_p or fd_p
            if fair:
                h["Edge Now %"] = f"{(fair * american_to_decimal(mine[side]) - 1) * 100:.2f}"
    HISTORY.append(h)


def save_history():
    if not HISTORY:
        return
    new = not os.path.isfile(HISTORY_FILE) or os.path.getsize(HISTORY_FILE) == 0
    if not new:
        with open(HISTORY_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != HISTORY_FIELDS:      # new book columns: rewrite once with the new header
                old = list(reader)
                with open(HISTORY_FILE + ".tmp", "w", newline="", encoding="utf-8") as g:
                    w = csv.DictWriter(g, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(old)
                os.replace(HISTORY_FILE + ".tmp", HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(HISTORY)
    print(f"📈 {len(HISTORY)} line-history snapshots saved to {HISTORY_FILE}")


def update_closing_lines(pending_rows, lines):
    """Snapshot current price + fair probs for pending plays on this event.
    Overwritten each run until tip, so the last pre-game run is the close.
    Records both the consensus close and FanDuel's devigged close, so the two
    methods can be judged against the SAME yardstick."""
    updated = 0
    now = datetime.now(timezone.utc)
    for row in pending_rows:
        start = parse_iso(row.get("Commence Time"))
        if start is not None and (start - now) < timedelta(minutes=CLOSE_MIN_LEAD_MINUTES):
            continue   # never take a "close" within 2 minutes of (or after) tip-off
        m_key = DISPLAY_TO_KEY.get(row["Market"])
        line = safe_float(row["Line"])
        if not m_key or line is None:
            continue
        players = {p for (mk, p, pt) in lines if mk == m_key and pt == line}
        player = match_name(row["Player"], players)
        side = row["Side"].strip().title()
        if player is None:
            _steam_line(row, lines, m_key, side)
            seen = row.get("Last Seen Odds", "")
            _history_row(row, "line moved" if seen.startswith("line:") else "not listed",
                         seen_line=seen[5:] if seen.startswith("line:") else "")
            continue
        quotes = lines[(m_key, player, line)]
        bet_dec = american_to_decimal(parse_american(row["Odds"]))
        got = False

        bet_bkey = next((k for k, q in quotes.items() if q["title"] == row["Bookmaker"]), None)
        fp = fair_prob(quotes, side, exclude=bet_bkey)
        if fp is not None:
            row["Close Fair %"] = f"{fp[0] * 100:.1f}"
            row["CLV %"] = f"{(fp[0] * bet_dec - 1) * 100:.2f}"
            got = True

        fd_p = fd_fair(quotes, side)
        if fd_p is not None and row["Bookmaker"] != "FanDuel":
            row["Close FD Fair %"] = f"{fd_p * 100:.1f}"
            row["CLV FD %"] = f"{(fd_p * bet_dec - 1) * 100:.2f}"
            got = True

        nq = quotes.get("novig")
        if nq and nq["fresh"] and "Over" in nq and "Under" in nq and row["Bookmaker"] != "Novig":
            nv_p = devigged(nq, side)                       # Novig exchange close, the sharpest benchmark
            row["Novig Close Fair %"] = f"{nv_p * 100:.1f}"
            row["Novig CLV %"] = f"{(nv_p * bet_dec - 1) * 100:.2f}"

        st = study_fair(quotes, side, exclude=bet_bkey)   # shadow close under study weights
        if st is not None:
            row["Study Close Fair %"] = f"{st[0] * 100:.1f}"
            row["Study CLV %"] = f"{(st[0] * bet_dec - 1) * 100:.2f}"

        _history_row(row, "ok", quotes, side, fp[0] if fp else None, fd_p)
        if got:
            row["Close Time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for q in quotes.values():
                if q["title"] == row["Bookmaker"] and q.get(side) is not None:
                    row["Close Odds"] = fmt_american(q[side])
                    _steam_price(row, q[side], fp[0] if fp else fd_p)
            updated += 1
    return updated


# ------------------------------------------------------------------ last-10 hit rate (ESPN, free)
# For rule plays only: how many of the player's last 10 games landed on your side of the line.
# Uses ESPN's public game logs (same source as the grader), so it costs no Odds API credits.
# Any failure (name mismatch, missing data, ESPN down) just leaves the L10 columns blank.
L10_GAMES = 10
ESPN_WEB = "https://site.web.api.espn.com/apis/common/v3/sports"
# market -> list of recipes (summed); each part = (gamelog stat names, gamelog labels)
L10_STATS = {
    "Points": [(("points",), ("PTS",))],
    "Rebounds": [(("totalRebounds", "rebounds"), ("REB",))],
    "Assists": [(("assists",), ("AST", "A"))],
    "Threes": [(("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "threePointFieldGoalsMade"), ("3PT", "3PM"))],
    "Points Rebounds": [(("points",), ("PTS",)), (("totalRebounds", "rebounds"), ("REB",))],
    "Points Rebounds Assists": [(("points",), ("PTS",)), (("totalRebounds", "rebounds"), ("REB",)),
                                (("assists",), ("AST",))],
    "Shots On Goal": [(("shotsTotal", "shots", "shotsOnGoal"), ("S", "SOG"))],
    "Total Saves": [(("saves",), ("SV", "SAVES"))],
    "Pass Yds": [(("passingYards",), ())],
    "Pass Attempts": [(("passingAttempts",), ())],
    "Rush Yds": [(("rushingYards",), ())],
    "Rush Attempts": [(("rushingAttempts",), ("CAR",))],
    "Reception Yds": [(("receivingYards",), ())],
    "Receptions": [(("receptions",), ("REC",))],
}
_l10_cache = {"roster": {}, "log": {}, "games": {}}
_l10_warned = set()


def _l10_warn(key, msg):
    if key not in _l10_warned:
        _l10_warned.add(key)
        print(f"  L10: {msg}")


def _espn_get(url):
    try:
        res = SESSION.get(url, timeout=8)
        return res.json() if res.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def _l10_teams(row):
    """ESPN team ids for the row's game, via the same matching the grader uses."""
    import grader
    league = grader.ESPN_LEAGUES.get(row["Sport"])
    start = parse_iso(row.get("Commence Time"))
    if not league or not start:
        return league, []
    key = (row["Sport"], row["Game"], row["Commence Time"])
    if key not in _l10_cache["games"]:
        d = start.astimezone(grader.ET).date()
        events = [ev for o in (-1, 0, 1)
                  for ev in grader.scoreboard(*league, (d + timedelta(days=o)).strftime("%Y%m%d"))]
        ev = grader.find_game(row["Game"], events, start, exact_start=True,
                              college=league[1] in grader.COLLEGE_LEAGUES)
        comps = ((ev or {}).get("competitions") or [{}])[0].get("competitors", [])
        _l10_cache["games"][key] = [c.get("team", {}).get("id") for c in comps if c.get("team", {}).get("id")]
    return league, _l10_cache["games"][key]


def _l10_athlete(row, league, team_ids):
    """ESPN athlete id for the row's player, from the two teams' rosters."""
    people = {}
    for tid in team_ids:
        ck = (league, tid)
        if ck not in _l10_cache["roster"]:
            data = _espn_get(f"https://site.api.espn.com/apis/site/v2/sports/{league[0]}/{league[1]}/teams/{tid}/roster") or {}
            found = {}
            for a in data.get("athletes", []):
                for p in (a.get("items") or [a]):          # NFL groups players by unit
                    if p.get("id") and p.get("displayName"):
                        found[p["displayName"]] = p["id"]
            _l10_cache["roster"][ck] = found
        people.update(_l10_cache["roster"][ck])
    name = match_name(row["Player"], list(people))
    return people.get(name) if name else None


def _l10_value(raw):
    if raw in (None, "", "--", "-"):
        return None
    s = str(raw).strip()
    if "-" in s[1:] or "/" in s:                     # "3-7" made-attempted -> 3
        s = s.replace("/", "-").split("-")[0]
    return safe_float(s)


def _l10_games(league, athlete_id):
    """Most recent games first: list of {stat key: raw value} for regular season / postseason games."""
    ck = (league, athlete_id)
    if ck in _l10_cache["log"]:
        return _l10_cache["log"][ck]
    data = _espn_get(f"{ESPN_WEB}/{league[0]}/{league[1]}/athletes/{athlete_id}/gamelog") or {}
    names, labels = data.get("names") or [], data.get("labels") or []
    info = data.get("events") or {}
    games = []
    for st in data.get("seasonTypes") or []:
        if "pre" in (st.get("displayName") or "").lower():
            continue                                     # skip preseason
        for cat in st.get("categories") or []:
            if cat.get("type") not in (None, "event"):
                continue                                 # skip monthly totals / averages
            for ev in cat.get("events") or []:
                stats = ev.get("stats") or []
                if not stats:
                    continue
                row = {}
                for i, v in enumerate(stats):
                    if i < len(names):
                        row.setdefault(("n", names[i]), v)
                    if i < len(labels):
                        row.setdefault(("l", labels[i]), v)
                when = (info.get(str(ev.get("eventId"))) or {}).get("gameDate") or ""
                games.append((when, row))
    if not games and data:
        _l10_warn(("shape", league), f"no games parsed from ESPN game log for {league[1]} (keys: {sorted(data)[:8]})")
    games.sort(key=lambda g: g[0], reverse=True)
    _l10_cache["log"][ck] = [g for _, g in games]
    return _l10_cache["log"][ck]


def _l10_stat(game, recipe):
    total = 0.0
    for names, labels in recipe:
        val = None
        for n in names:
            val = _l10_value(game.get(("n", n)))
            if val is not None:
                break
        if val is None:
            for lab in labels:
                val = _l10_value(game.get(("l", lab)))
                if val is not None:
                    break
        if val is None:
            return None
        total += val
    return total


def add_l10(rows):
    """Fill 'L10 Hits' ('6/10') and 'L10 Avg' on rule plays. Never raises."""
    for row in rows:
        try:
            if not any(f(row) for _, f in STEAM_RULES):
                continue
            recipe = L10_STATS.get(row["Market"])
            line = safe_float(row["Line"])
            if not recipe or line is None:
                continue
            league, teams = _l10_teams(row)
            if not teams:
                _l10_warn(("game", row["Game"]), f"no ESPN game found for {row['Game']}")
                continue
            aid = _l10_athlete(row, league, teams)
            if not aid:
                _l10_warn(("player", row["Player"]), f"{row['Player']} not found on ESPN rosters")
                continue
            vals = []
            for g in _l10_games(league, aid):
                v = _l10_stat(g, recipe)
                if row["Sport"].startswith("basketball"):
                    mins = _l10_value(g.get(("l", "MIN")))
                    if mins is not None and mins <= 0:
                        continue                          # DNP
                if v is not None:
                    vals.append(v)
                if len(vals) == L10_GAMES:
                    break
            if not vals:
                _l10_warn(("stat", row["Market"]), f"no {row['Market']} values in ESPN game logs")
                continue
            under = row["Side"].strip().lower() == "under"
            hits = sum((v < line) if under else (v > line) for v in vals)
            row["L10 Hits"] = f"{hits}/{len(vals)}"
            row["L10 Avg"] = f"{sum(vals) / len(vals):.1f}"
        except Exception as e:                            # never let form lookups break a scan
            _l10_warn(("err", type(e).__name__), f"lookup error {type(e).__name__}: {e}")


def _l10_bit(r, long=False):
    if not r.get("L10 Hits"):
        return ""
    side = "under" if r["Side"] == "Under" else "over"
    if long:
        return f"📊 **L10: {r['L10 Hits']} {side} {r['Line']}** • avg {r['L10 Avg']}"
    return f" • L10 {r['L10 Hits']}"


# ------------------------------------------------------------------ discord
def fmt_start(commence):
    dt = parse_iso(commence)
    if dt is None:
        return ""
    ct = dt.astimezone(CT)
    return f"{ct.strftime('%a')} {ct.strftime('%I:%M %p').lstrip('0')} CT"


DIGEST_BUDGET = 3500   # max UTF-16 units of play text per Discord message (limit is 4,096)


def _short_side(r):
    return f"{'U' if r['Side'] == 'Under' else 'O'} {r['Line']} {r['Market']}"


def _odds_pct(odds):
    """'+108' -> '+108 (48%)': American odds followed by the implied win chance, the way Novig shows prices."""
    try:
        return f"{odds} ({american_to_prob(parse_american(odds)) * 100:.0f}%)"
    except (TypeError, ValueError, ZeroDivisionError):
        return str(odds)


BET_STATE = "ks"   # fills the {state} placeholder some books use in their links (e.g. sports.{state}.betmgm.com)


BET_WAGER = 35     # fills the {wager} placeholder (Novig puts the stake in its bet-slip link)


def _bet_link(r):
    """' • [Bet @ DraftKings](url)' when the Odds API returned a link for this play, else ''."""
    url = ((r.get("_link") or r.get("Bet Link") or "").strip()
           .replace("{state}", BET_STATE).replace("{wager}", str(BET_WAGER)))
    url = re.sub(r"/?\{[^}]*\}", "", url)   # drop any other unfilled placeholder rather than send a broken link
    if not url.startswith("http"):
        return ""
    return f" • [Bet @ {r['Bookmaker']}]({url.replace(' ', '%20').replace(')', '%29')})"


def _method_word(r):
    if r.get("Method") == "fd":
        return "FD" if (r.get("Cons Edge %") or "").strip() else "FD SOLO"
    return {"both": "BOTH", "consensus": "CONS"}.get(r.get("Method"), "")


def _cal_bit(r):
    ce = safe_float(r.get("Cal Edge %"))
    if ce is None:
        return ""
    return f"🏹 cal {ce:+.2f}%" if ce >= CAL_MIN_EDGE else f"cal {ce:+.2f}%"


def _study_bit(r):
    st = safe_float(r.get("Study Edge %"))
    if st is None:
        return "study n/a"
    return f"{'✅' if st >= STUDY_AGREE_EDGE else '❌'} study {st:+.2f}%"


def send_discord_digest(new_rows, run_ts):
    """Bet-list digest: 🍀 Lucky plays, then ⚡ Retail plays, each with full detail. Every other logged
    play is summarized in one line (still logged and graded, just not recommended)."""
    if not DISCORD_WEBHOOK_URL or not new_rows:
        return
    focus = sorted([r for r in new_rows if meets_lucky(r)], key=lambda r: -float(r["Edge %"]))
    retail = sorted([r for r in new_rows if not meets_lucky(r) and meets_retail(r)], key=lambda r: -float(r["Edge %"]))
    rest = [r for r in new_rows if not meets_lucky(r) and not meets_retail(r)]
    tag_rules = [("⚡", meets_retail), ("🚒", meets_calienta), ("🏀", meets_pj), ("🇺🇸", meets_merica)]
    n_focus, n_retail = len(focus), len(retail)

    def detail(r, emoji):
        also = "".join(e for e, f in tag_rules if e != emoji and f(r))
        return (f"{emoji} **{r['Player']}** {_short_side(r)}\n"
                f"**{_odds_pct(r['Odds'])} @ {r['Bookmaker']}** • **{r['Kelly Units']}u (${r['Bet Amount']})** • "
                f"edge {float(r['Edge %']):.2f}% • {_method_word(r)}{_bet_link(r)}\n"
                + (f"{_l10_bit(r, True)}\n" if r.get("L10 Hits") else "") +
                f"{r['Game']} • 🕒 {fmt_start(r['Commence Time'])}" + (f" • also {also}" if also else ""))

    entries = []   # (text, is_play)
    if focus:
        entries.append((f"**🍀 LUCKY — {n_focus} play{'s' if n_focus != 1 else ''}**", False))
        entries += [(detail(r, "🍀"), True) for r in focus]
    else:
        entries.append(("**🍀 No Lucky plays this scan**", False))
    if retail:
        entries.append((f"**⚡ RETAIL — {n_retail} play{'s' if n_retail != 1 else ''}**", False))
        entries += [(detail(r, "⚡"), True) for r in retail]
    if rest:
        tags = " • ".join(f"{e} {sum(f(r) for r in rest)}" for e, f in tag_rules[1:] if any(f(r) for r in rest))
        entries.append((f"· {len(rest)} other play{'s' if len(rest) != 1 else ''} logged, not on the bet list"
                        + (f" ({tags})" if tags else ""), False))

    def u16(text):
        return len(text.encode("utf-16-le")) // 2

    header = f"**{credit_summary()}**\n\n"
    pages, cur, size = [], [], u16(header)
    for text, is_play in entries:
        sep = "\n\n"
        add = u16(text) + len(sep)
        if cur and size + add > DIGEST_BUDGET:
            pages.append(cur)
            cur, size = [], 0
        cur.append((text, is_play, sep))
        size += add
    if cur:
        pages.append(cur)

    total = len(new_rows)
    title_base = f"🍀 {n_focus} Lucky • ⚡ {n_retail} Retail / {total} logged"
    first = 1
    for i, page in enumerate(pages):
        body = ""
        for j, (text, is_play, sep) in enumerate(page):
            body += ("" if j == 0 else sep) + text
        n_plays = sum(p for _, p, _ in page)
        last = first + n_plays - 1
        part = f" (Part {i + 1}/{len(pages)})" if len(pages) > 1 else ""
        embed = {
            "title": title_base + part,
            "description": (header if i == 0 else "") + body,
            "color": 0x2ECC71 if n_focus else (0xF1C40F if n_retail else 0x808080),   # green Lucky, yellow Retail
            "footer": {"text": (f"Plays {first}–{last} of {n_focus + n_retail} bet-list plays • " if n_plays else "") + f"Scanned {run_ts} CT"},
        }
        first = last + 1 if n_plays else first
        try:
            resp = SESSION.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
            if getattr(resp, "status_code", 204) >= 300:
                print(f"Discord rejected digest part {i + 1}: {resp.status_code} {getattr(resp, 'text', '')[:200]}")
        except requests.RequestException as e:
            print(f"Discord error: {e}")


def send_discord_no_plays(run_ts, games_scanned, edges_found):
    """Short heartbeat when a scan logs nothing new, so every scan shows up with its credit cost."""
    if not DISCORD_WEBHOOK_URL:
        return
    if games_scanned == 0:
        detail = f"No games starting in the next {LOOKAHEAD_HOURS:g} hours."
    elif edges_found:
        detail = f"{games_scanned} games scanned; {edges_found} edges seen, all already logged."
    else:
        detail = f"{games_scanned} games scanned; nothing met the edge filters."
    embed = {
        "title": "😴 No new plays",
        "description": f"**{credit_summary()}**\n{detail}",
        "color": 0x808080,
        "footer": {"text": f"Scanned {run_ts} CT"},
    }
    try:
        SESSION.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
    except requests.RequestException as e:
        print(f"Discord error: {e}")


# ------------------------------------------------------------------ main
def send_discord_steam(run_ts):
    """🔥 Rule plays whose price (or line) at your book got worse since the last scan."""
    if not STEAM_DISCORD or not DISCORD_WEBHOOK_URL or not STEAM_ALERTS:
        return
    lines_out = []
    for a in sorted(STEAM_ALERTS, key=lambda a: a["row"]["Commence Time"]):
        r = a["row"]
        head = f"{a['tags']} **{r['Player']} {_short_side(r)}**"
        when = f"🕒 {fmt_start(r['Commence Time'])}"
        if a["kind"] == "price":
            edge = f" • edge now {a['edge']:.1f}%" if a["edge"] is not None else ""
            lines_out.append(f"{head} • {r['Bookmaker']} **{a['from']} → {a['to']}** "
                             f"(logged {r['Odds']}){edge} • {when}")
        else:
            px = f" at {a['price']}" if a["price"] else ""
            lines_out.append(f"{head} • {r['Bookmaker']} line moved **{a['from']} → {a['to']}**{px} "
                             f"(logged {r['Odds']}) • {when}")
    n = len(lines_out)
    pages, cur = [], ""
    for ln in lines_out:
        if cur and len(cur) + len(ln) + 2 > 3500:
            pages.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + ln
    pages.append(cur)
    for i, body in enumerate(pages):
        part = f" (Part {i + 1}/{len(pages)})" if len(pages) > 1 else ""
        embed = {"title": f"🔥 Steam — {n} rule play{'s' if n != 1 else ''} getting more expensive{part}",
                 "description": body, "color": 0xFF6B00,
                 "footer": {"text": f"Since last scan • threshold {STEAM_MIN_PP:g} win-% pts • {run_ts} CT"}}
        try:
            SESSION.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        except requests.RequestException as e:
            print(f"Discord error: {e}")


def run():
    if not API_KEY:
        print("CRITICAL ERROR: ODDS_API_KEY missing.")
        return

    rows = load_rows()
    cal = fit_calibration(rows, datetime.now(timezone.utc).astimezone(CT).strftime("%Y-%m-%d"))
    print("Calibration (win-chance points): " + ", ".join(
        f"{k[0].split('_')[-1]} {k[1]} {v:+.1f}" for k, v in cal.items() if isinstance(k, tuple)))
    seen = {play_key(r["Game"], r["Market"], r["Player"], r["Side"], r["Line"]) for r in rows}
    exposure = defaultdict(float)
    directions = defaultdict(set)   # (game, player) -> {"over","under"} across ALL markets
    players_logged = set()          # (game, player) already holding a play
    pending_by_event = defaultdict(list)
    for r in rows:
        exposure[(r["Game"].strip().lower(), normalize_name(r["Player"]))] += safe_float(r["Kelly Units"], 0.0)
        directions[(r["Game"].strip().lower(), normalize_name(r["Player"]))].add(r["Side"].strip().lower())
        players_logged.add((r["Game"].strip().lower(), normalize_name(r["Player"])))
        if r["Result"] == "PENDING" and r["Event ID"]:
            pending_by_event[r["Event ID"]].append(r)

    now_utc = datetime.now(timezone.utc)
    now_ct = now_utc.astimezone(CT)
    end_utc = now_utc + timedelta(hours=LOOKAHEAD_HOURS)
    run_ts = now_ct.strftime("%Y-%m-%d %H:%M:%S")
    print(f"--- EV Prop Scanner {run_ts} CT ---")

    new_rows, edges_found, closes = [], 0, 0
    games_scanned = 0

    for sport, markets in SPORTS_CONFIG.items():
        if sport in PAUSED_SPORTS:
            print(f"\n{sport}: paused (in PAUSED_SPORTS), skipped")
            continue
        events = get_json(f"{BASE_URL}/sports/{sport}/events", {"apiKey": API_KEY})
        if not events:
            continue
        upcoming = []
        for ev in events:
            start = parse_iso(ev.get("commence_time"))
            # Only games that have NOT started (in-play props create phantom edges)
            if start is not None and now_utc < start <= end_utc:
                upcoming.append((ev, start))
        if not upcoming:
            continue
        print(f"\n{sport}: {len(upcoming)} upcoming games")
        games_scanned += len(upcoming)

        for ev, start in upcoming:
            if low_quota():
                print(f"    credits below reserve ({QUOTA['remaining']} < {QUOTA_RESERVE}), skipping remaining games")
                break
            game = f"{ev['away_team']} @ {ev['home_team']}"
            data = get_json(
                f"{BASE_URL}/sports/{sport}/events/{ev['id']}/odds",
                {"apiKey": API_KEY, "markets": markets, "bookmakers": KS_BOOKS, "oddsFormat": "american",
                 "includeLinks": "true", "includeSids": "true"},
            )
            if not data:
                continue
            lines = build_lines(data, datetime.now(timezone.utc))
            closes += update_closing_lines(pending_by_event.get(ev["id"], []), lines)

            candidates = sorted(find_edges(lines), key=lambda c: c["edge"], reverse=True)
            edges_found += len(candidates)
            for c in candidates:
                market = format_market_name(c["m_key"])
                key = play_key(game, market, c["player"], c["side"], c["pt"])
                if key in seen:
                    continue  # also keeps only the best-priced book per play
                ek = (game.lower(), normalize_name(c["player"]))
                if ONE_PLAY_PER_PLAYER and ek in players_logged:
                    continue  # candidates are sorted by edge, so the best play was taken first
                if directions[ek] - {c["side"].lower()}:
                    continue  # same player, same game: every bet must point the same direction
                units = min(kelly_units(c["p"], c["dec"]), MAX_UNITS_PER_PLAY,
                            MAX_UNITS_PER_PLAYER_GAME - exposure[ek])
                if units < MIN_UNITS:
                    continue
                seen.add(key)
                directions[ek].add(c["side"].lower())
                players_logged.add(ek)
                exposure[ek] += units
                row = {
                    "Timestamp": run_ts, "Sport": sport, "Event ID": ev["id"],
                    "Commence Time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "Game": game, "Market": market, "Player": c["player"],
                    "Side": c["side"], "Line": f"{c['pt']:g}", "Bookmaker": c["book"],
                    "Odds": fmt_american(c["price"]),
                    "True Prob %": f"{c['p'] * 100:.1f}", "Edge %": f"{c['edge'] * 100:.2f}",
                    "Kelly Units": f"{units:.2f}", "Bet Amount": f"{units * UNIT_SIZE:.2f}",
                    "Fair Books": ", ".join(c["used"]) if c["used"] else "FanDuel",
                    "Close Odds": "", "Close Fair %": "", "CLV %": "",
                    "Result": "PENDING", "Net Units": "0.00", "Actual": "",
                    "Method": c["method"],
                    "FD Edge %": f"{c['fd_edge'] * 100:.2f}" if c["fd_edge"] is not None else "",
                    "Cons Edge %": f"{c['cons_edge'] * 100:.2f}" if c["cons_edge"] is not None else "",
                    "Close FD Fair %": "", "CLV FD %": "", "Close Time": "",
                    "Study Fair %": f"{c['study_p'] * 100:.1f}" if c["study_p"] is not None else "",
                    "Study Edge %": f"{(c['study_p'] * c['dec'] - 1) * 100:.2f}" if c["study_p"] is not None else "",
                    "Study Books": ", ".join(c["study_used"]),
                    "Scanner Version": SCANNER_VERSION,
                    "Study Close Fair %": "", "Study CLV %": "",
                    "_link": c.get("link") or "",
                    "Bet Link": c.get("link") or "",
                    "Bet Sids": c.get("sids") or "",
                    "_fair": " | ".join(x for x in (
                        f"FD {prob_to_american(c['fd_p'])}" if c["fd_p"] is not None else "",
                        f"Cons {prob_to_american(c['cons_p'])} ({len(c['used'])} bks)" if c["cons_p"] is not None else "",
                    ) if x),
                }
                cp, ce = calibrated_prob_edge(row, cal)
                row["Cal Prob %"] = f"{cp:.1f}" if cp is not None else ""
                row["Cal Edge %"] = f"{ce:.2f}" if ce is not None else ""
                new_rows.append(row)
                print(f"  + [{c['method']}] {row['Edge %']}% {row['Player']} {row['Side']} {row['Line']} {market} {row['Odds']} @ {row['Bookmaker']}")

    if new_rows:
        add_l10(new_rows)
        n_l10 = sum(bool(r.get("L10 Hits")) for r in new_rows)
        print(f"📊 L10 hit rates found for {n_l10} of {sum(any(f(r) for _, f in STEAM_RULES) for r in new_rows)} rule plays")
    if new_rows or closes:
        rows.extend(new_rows)
        save_rows(rows)
    if new_rows:
        send_discord_digest(new_rows, run_ts)
    else:
        send_discord_no_plays(run_ts, games_scanned, edges_found)
    save_history()
    send_discord_steam(run_ts)
    if STEAM_ALERTS:
        print(f"🔥 {len(STEAM_ALERTS)} steam moves {'posted' if STEAM_DISCORD else 'tracked (Discord posting off)'}")
    print(f"\nDone. {edges_found} edges seen, {len(new_rows)} new plays logged, "
          f"{closes} closing-line snapshots updated.\n{credit_summary()}")


if __name__ == "__main__":
    run()
