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
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from common import (
    CT, UNIT_SIZE, american_to_decimal, american_to_prob, fmt_american,
    load_rows, match_name, normalize_name, parse_american, parse_iso,
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
    "draftkings": 1.0,
    "betmgm": 0.75,
    "caesars": 0.75,
    "espnbet": 0.75,
}
ANCHOR_BOOKS = {"novig", "fanduel"}
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

SPORTS_CONFIG = {
    "basketball_wnba": "player_points,player_rebounds,player_assists,player_points_rebounds,player_points_rebounds_assists,player_threes",
    "basketball_nba": "player_points,player_rebounds,player_assists,player_points_rebounds,player_points_rebounds_assists,player_threes",
    "basketball_nba_preseason": "player_points,player_rebounds,player_assists,player_points_rebounds,player_points_rebounds_assists,player_threes",
    "icehockey_nhl": "player_points,player_assists,player_shots_on_goal,player_total_saves",
    "icehockey_nhl_preseason": "player_points,player_assists,player_shots_on_goal,player_total_saves",
    "americanfootball_nfl": "player_pass_yds,player_pass_attempts,player_rush_yds,player_rush_attempts,player_reception_yds,player_receptions",
    "americanfootball_ncaaf": "player_pass_yds,player_pass_attempts,player_rush_yds,player_rush_attempts,player_reception_yds,player_receptions",
}


def format_market_name(market_key):
    return market_key.replace("player_", "").replace("_", " ").title()


DISPLAY_TO_KEY = {
    format_market_name(k): k
    for markets in SPORTS_CONFIG.values() for k in markets.split(",")
}

QUOTA = {"remaining": "?", "used": "?"}
SESSION = requests.Session()


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
        for bkey, q in quotes.items():
            if not q["fresh"]:
                continue
            for side in ("Over", "Under"):
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

                out.append({
                    "m_key": m_key, "player": player, "raw": q["raw"], "pt": pt,
                    "side": side, "book": q["title"], "price": price, "dec": dec,
                    "p": p, "edge": edge, "used": used, "method": method,
                    "fd_p": fd_p, "fd_edge": fd_edge, "cons_p": cons_p, "cons_edge": cons_edge,
                })
    return out


def update_closing_lines(pending_rows, lines):
    """Snapshot current price + fair probs for pending plays on this event.
    Overwritten each run until tip, so the last pre-game run is the close.
    Records both the consensus close and FanDuel's devigged close, so the two
    methods can be judged against the SAME yardstick."""
    updated = 0
    for row in pending_rows:
        m_key = DISPLAY_TO_KEY.get(row["Market"])
        line = safe_float(row["Line"])
        if not m_key or line is None:
            continue
        players = {p for (mk, p, pt) in lines if mk == m_key and pt == line}
        player = match_name(row["Player"], players)
        if player is None:
            continue
        quotes = lines[(m_key, player, line)]
        side = row["Side"].strip().title()
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

        if got:
            for q in quotes.values():
                if q["title"] == row["Bookmaker"] and side in q:
                    row["Close Odds"] = fmt_american(q[side])
            updated += 1
    return updated


# ------------------------------------------------------------------ discord
def fmt_start(commence):
    dt = parse_iso(commence)
    if dt is None:
        return ""
    ct = dt.astimezone(CT)
    return f"{ct.strftime('%a')} {ct.strftime('%I:%M %p').lstrip('0')} CT"


def send_discord_digest(new_rows, run_ts):
    if not DISCORD_WEBHOOK_URL or not new_rows:
        return
    plays = sorted(new_rows, key=lambda r: float(r["Edge %"]), reverse=True)
    chunk_size = 15
    total_chunks = (len(plays) + chunk_size - 1) // chunk_size
    for i in range(0, len(plays), chunk_size):
        out = []
        for r in plays[i:i + chunk_size]:
            edge = float(r["Edge %"])
            icon = "🔥" if edge >= 5.0 else ("💎" if edge >= 3.5 else "⬜")
            tag = {"fd": "🎯 FD", "consensus": "🧮 CONS", "both": "🎯🧮 BOTH"}.get(r.get("Method"), "")
            out.append(
                f"{icon} **+{r['Edge %']}%** `{tag}` | **{r['Player']}** {r['Side']} {r['Line']} {r['Market']}\n"
                f"↳ **{r['Odds']}** @ {r['Bookmaker']} • **{r['Kelly Units']}u** (${r['Bet Amount']}) • *{r['_fair']}*\n"
                f"  *{r['Game']}* • 🕒 {fmt_start(r['Commence Time'])}"
            )
        part = f" (Part {i // chunk_size + 1}/{total_chunks})" if total_chunks > 1 else ""
        embed = {
            "title": f"🚨 +EV Prop Digest ({len(plays)} Plays){part}",
            "description": "\n\n".join(out)[:4000],
            "color": 65280,
            "footer": {"text": f"Scanned {run_ts} CT • Side-by-side test: FanDuel devig vs consensus"},
        }
        try:
            SESSION.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        except requests.RequestException as e:
            print(f"Discord error: {e}")


# ------------------------------------------------------------------ main
def run():
    if not API_KEY:
        print("CRITICAL ERROR: ODDS_API_KEY missing.")
        return

    rows = load_rows()
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
    end_utc = now_ct.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(timezone.utc)
    run_ts = now_ct.strftime("%Y-%m-%d %H:%M:%S")
    print(f"--- EV Prop Scanner {run_ts} CT ---")

    new_rows, edges_found, closes = [], 0, 0

    for sport, markets in SPORTS_CONFIG.items():
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

        for ev, start in upcoming:
            game = f"{ev['away_team']} @ {ev['home_team']}"
            data = get_json(
                f"{BASE_URL}/sports/{sport}/events/{ev['id']}/odds",
                {"apiKey": API_KEY, "markets": markets, "bookmakers": KS_BOOKS, "oddsFormat": "american"},
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
                    "Close FD Fair %": "", "CLV FD %": "",
                    "_fair": " | ".join(x for x in (
                        f"FD {prob_to_american(c['fd_p'])}" if c["fd_p"] is not None else "",
                        f"Cons {prob_to_american(c['cons_p'])} ({len(c['used'])} bks)" if c["cons_p"] is not None else "",
                    ) if x),
                }
                new_rows.append(row)
                print(f"  + [{c['method']}] {row['Edge %']}% {row['Player']} {row['Side']} {row['Line']} {market} {row['Odds']} @ {row['Bookmaker']}")

    if new_rows or closes:
        rows.extend(new_rows)
        save_rows(rows)
    send_discord_digest(new_rows, run_ts)
    print(f"\nDone. {edges_found} edges seen, {len(new_rows)} new plays logged, "
          f"{closes} closing-line snapshots updated. API quota remaining: {QUOTA['remaining']}")


if __name__ == "__main__":
    run()
