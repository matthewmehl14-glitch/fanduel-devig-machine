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

MIN_FAIR_BOOKS = 2        # two-way books (excluding target) needed for a fair line
MAX_ANCHOR_GAP = 0.04     # skip line if Novig and FanDuel disagree by > 4 pts of prob
STALE_MINUTES = 10        # ignore quotes not refreshed within this window
MIN_EDGE = 0.02
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
    if len(anchors) == 2 and abs(anchors["novig"] - anchors["fanduel"]) > MAX_ANCHOR_GAP:
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


def find_edges(lines):
    out = []
    for (m_key, player, pt), quotes in lines.items():
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
                fp = fair_prob(quotes, side, exclude=bkey)
                if fp is None:
                    continue
                p, used = fp
                edge = p * dec - 1
                if MIN_EDGE <= edge <= MAX_EDGE:
                    out.append({
                        "m_key": m_key, "player": player, "raw": q["raw"], "pt": pt,
                        "side": side, "book": q["title"], "price": price, "dec": dec,
                        "p": p, "edge": edge, "used": used,
                    })
    return out


def update_closing_lines(pending_rows, lines):
    """Snapshot current price + fair prob for pending plays on this event.
    Overwritten each run until tip, so the last pre-game run is the close."""
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
        fp = fair_prob(quotes, side)
        if fp is None:
            continue
        close_fair = fp[0]
        bet_dec = american_to_decimal(parse_american(row["Odds"]))
        row["Close Fair %"] = f"{close_fair * 100:.1f}"
        row["CLV %"] = f"{(close_fair * bet_dec - 1) * 100:.2f}"
        for q in quotes.values():
            if q["title"] == row["Bookmaker"] and side in q:
                row["Close Odds"] = fmt_american(q[side])
        updated += 1
    return updated


# ------------------------------------------------------------------ discord
def send_discord_digest(new_rows, run_ts):
    if not DISCORD_WEBHOOK_URL or not new_rows:
        return
    plays = sorted(new_rows, key=lambda r: float(r["Edge %"]), reverse=True)
    chunk_size = 15
    total_chunks = (len(plays) + chunk_size - 1) // chunk_size
    for i in range(0, len(plays), chunk_size):
        out = []
        for r in plays[i:i + chunk_size]:
            icon = "🔥" if float(r["Edge %"]) >= 5.0 else "💎"
            out.append(
                f"{icon} **+{r['Edge %']}%** | **{r['Player']}** {r['Side']} {r['Line']} {r['Market']}\n"
                f"↳ **{r['Odds']}** @ {r['Bookmaker']} • **{r['Kelly Units']}u** (${r['Bet Amount']}) • *{r['_fair']}*\n"
                f"  *{r['Game']}*"
            )
        part = f" (Part {i // chunk_size + 1}/{total_chunks})" if total_chunks > 1 else ""
        embed = {
            "title": f"🚨 +EV Prop Digest ({len(plays)} Plays){part}",
            "description": "\n\n".join(out)[:4000],
            "color": 65280,
            "footer": {"text": f"Scanned {run_ts} CT • Weighted consensus devig (Novig/FD anchored)"},
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
    sides_logged = defaultdict(set)   # (game, market, player) -> {"over","under"}
    pending_by_event = defaultdict(list)
    for r in rows:
        exposure[(r["Game"].strip().lower(), normalize_name(r["Player"]))] += safe_float(r["Kelly Units"], 0.0)
        k = play_key(r["Game"], r["Market"], r["Player"], r["Side"], r["Line"])
        sides_logged[k[:3]].add(k[3])
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
                if sides_logged[key[:3]] - {key[3]}:
                    continue  # already holding the opposite side of this prop
                ek = (game.lower(), normalize_name(c["player"]))
                units = min(kelly_units(c["p"], c["dec"]), MAX_UNITS_PER_PLAY,
                            MAX_UNITS_PER_PLAYER_GAME - exposure[ek])
                if units < MIN_UNITS:
                    continue
                seen.add(key)
                sides_logged[key[:3]].add(key[3])
                exposure[ek] += units
                row = {
                    "Timestamp": run_ts, "Sport": sport, "Event ID": ev["id"],
                    "Commence Time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "Game": game, "Market": market, "Player": c["player"],
                    "Side": c["side"], "Line": f"{c['pt']:g}", "Bookmaker": c["book"],
                    "Odds": fmt_american(c["price"]),
                    "True Prob %": f"{c['p'] * 100:.1f}", "Edge %": f"{c['edge'] * 100:.2f}",
                    "Kelly Units": f"{units:.2f}", "Bet Amount": f"{units * UNIT_SIZE:.2f}",
                    "Fair Books": ", ".join(c["used"]),
                    "Close Odds": "", "Close Fair %": "", "CLV %": "",
                    "Result": "PENDING", "Net Units": "0.00", "Actual": "",
                    "_fair": f"Fair {prob_to_american(c['p'])} ({len(c['used'])} books)",
                }
                new_rows.append(row)
                print(f"  + {row['Edge %']}% {row['Player']} {row['Side']} {row['Line']} {market} {row['Odds']} @ {row['Bookmaker']}")

    if new_rows or closes:
        rows.extend(new_rows)
        save_rows(rows)
    send_discord_digest(new_rows, run_ts)
    print(f"\nDone. {edges_found} edges seen, {len(new_rows)} new plays logged, "
          f"{closes} closing-line snapshots updated. API quota remaining: {QUOTA['remaining']}")


if __name__ == "__main__":
    run()
