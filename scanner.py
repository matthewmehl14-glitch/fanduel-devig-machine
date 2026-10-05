"""
EV Prop Scanner v2

Changes vs v1:
  - Fair value = power-method devig across ALL fresh two-sided books (leave-one-out
    per target book), weighted average, with a peer-agreement (spread) gate
  - Any allowed book can be a target (set EXCLUDE_AS_TARGET to restrict)
  - Dedup key includes game date; best price per play is kept (not first book seen)
  - Exact normalized-name matching; strict, unambiguous fuzzy fallback only
  - Central time via zoneinfo (DST-safe) with a rule-based fallback for devices
    without tz data (e.g. some Pydroid installs)
  - Skips games already started; only scans games within LOOKAHEAD_HOURS
  - Stale-line filter using The Odds API last_update timestamps
  - Suspiciously large edges are flagged and given NO stake
  - Per-bet / per-game / per-player exposure caps (carried across runs via the CSV log)
  - Retry/backoff session, quota header tracking, Discord rate-limit handling
  - Clean CSV (Run ID column, no separator rows). An old-format log is renamed, not deleted.
"""
import os
import re
import csv
import time
import unicodedata
import requests
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from zoneinfo import ZoneInfo
    CENTRAL = ZoneInfo("America/Chicago")
except Exception:
    CENTRAL = None  # falls back to rule-based US Central offset below

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
API_KEY = os.environ.get('ODDS_API_KEY')
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')
UNIT_SIZE = 25.00

KS_BOOKS = 'fanduel,draftkings,betmgm,caesars,espnbet,novig'
ALLOWED_BOOKS = set(KS_BOOKS.split(','))
BOOK_WEIGHTS = {'fanduel': 1.5}      # weight in fair-value average (default 1.0)
EXCLUDE_AS_TARGET = set()            # e.g. {'novig'} to never alert on a book
CSV_FILENAME = 'ev_plays_log.csv'

# Fair-value / edge gates
MIN_FAIR_BOOKS = 3        # two-sided, fresh books (excluding the target) needed for fair value
MAX_PEER_SPREAD = 0.04    # max (high - low) devigged prob among peers for that side
MIN_EDGE = 0.03           # replaces old 2% edge floor + 2% outlier gate (tune to taste)
SUSPECT_EDGE = 0.12       # edges at/above this are flagged "SUSPECT" and not staked
MAX_STALE_MINUTES = 20    # last_update older than this => ignored (see note in is_fresh)
LOOKAHEAD_HOURS = float(os.environ.get('LOOKAHEAD_HOURS', 10))

# Staking
KELLY_FRACTION = 0.25
MAX_BET_UNITS = 2.0
MAX_GAME_UNITS = 4.0
MAX_PLAYER_UNITS = 2.0
MIN_BET_UNITS = 0.05

# Name matching
FUZZY_MIN = 0.92

# Quota safety: stop scanning if remaining credits drop below this
MIN_QUOTA_REMAINING = 25

# Per-event cost = number of markets (6 books <= 10 => counts as one "region").
SPORTS_CONFIG = {
    'basketball_wnba': 'player_points,player_rebounds,player_assists,player_points_rebounds,player_points_rebounds_assists,player_threes',
    'basketball_nba': 'player_points,player_rebounds,player_assists,player_points_rebounds,player_points_rebounds_assists,player_threes',
    'basketball_nba_preseason': 'player_points,player_rebounds,player_assists,player_points_rebounds,player_points_rebounds_assists,player_threes',
    'icehockey_nhl': 'player_points,player_assists,player_shots_on_goal,player_total_saves',
    'icehockey_nhl_preseason': 'player_points,player_assists,player_shots_on_goal,player_total_saves',
    'americanfootball_nfl': 'player_pass_yds,player_pass_attempts,player_rush_yds,player_rush_attempts,player_reception_yds,player_receptions',
    'americanfootball_ncaaf': 'player_pass_yds,player_pass_attempts,player_rush_yds,player_rush_attempts,player_reception_yds,player_receptions'
}

BASE_HEADER = ['Run ID', 'Timestamp', 'Game Date', 'Sport', 'Game', 'Market', 'Player', 'Side', 'Line',
               'Bookmaker', 'Odds', 'Fair Prob %', 'Edge %', 'Kelly Units', 'Bet Amount',
               'Peer Books', 'Peer Spread %', 'Flag']
# Result / Net Units are filled in by ev_grader_v2.py (scanner writes PENDING / 0.00)
CSV_HEADER = BASE_HEADER + ['Result', 'Net Units']
LOCK_PATH = CSV_FILENAME + '.lock'


# ----------------------------------------------------------------------------
# TIME HELPERS
# ----------------------------------------------------------------------------
def _nth_sunday(year, month, n):
    d = datetime(year, month, 1)
    first_sunday = d + timedelta(days=(6 - d.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def to_central(dt_utc):
    """Convert an aware UTC datetime to US Central (DST-aware)."""
    if CENTRAL is not None:
        return dt_utc.astimezone(CENTRAL)
    y = dt_utc.year
    dst_start = _nth_sunday(y, 3, 2).replace(hour=8, tzinfo=timezone.utc)   # 2:00 CST
    dst_end = _nth_sunday(y, 11, 1).replace(hour=7, tzinfo=timezone.utc)    # 2:00 CDT
    offset = -5 if dst_start <= dt_utc < dst_end else -6
    return dt_utc.astimezone(timezone(timedelta(hours=offset)))


def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_fresh(updated, now_utc):
    """
    Missing timestamp => treated as fresh (can't verify).
    Note: The Odds API's last_update reflects when a book's odds last CHANGED,
    so a line that simply hasn't moved will look older. Raise MAX_STALE_MINUTES
    if this filters out too many legitimate plays.
    """
    if updated is None:
        return True
    return (now_utc - updated) <= timedelta(minutes=MAX_STALE_MINUTES)


# ----------------------------------------------------------------------------
# ODDS MATH
# ----------------------------------------------------------------------------
def american_to_prob(odds):
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)


def american_to_decimal(odds):
    if odds > 0:
        return (odds / 100) + 1
    return (100 / abs(odds)) + 1


def prob_to_american(prob):
    if prob <= 0 or prob >= 1:
        return "N/A"
    if prob >= 0.5:
        odds = (prob / (1 - prob)) * -100
    else:
        odds = ((1 - prob) / prob) * 100
    return f"+{int(round(odds))}" if odds > 0 else str(int(round(odds)))


def devig_power(p_a, p_b):
    """
    Power-method devig: find k such that p_a^k + p_b^k = 1.
    Handles lopsided two-way markets better than proportional scaling.
    """
    if p_a <= 0 or p_b <= 0 or p_a >= 1 or p_b >= 1:
        return None
    lo, hi = 0.01, 20.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if p_a ** mid + p_b ** mid > 1:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2
    fa, fb = p_a ** k, p_b ** k
    s = fa + fb
    return fa / s, fb / s


def format_market_name(market_key):
    return market_key.replace('player_', '').replace('_', ' ').title()


# ----------------------------------------------------------------------------
# NAME HANDLING
# ----------------------------------------------------------------------------
def normalize_name(name):
    if not name:
        return ""
    name = unicodedata.normalize('NFKD', str(name)).encode('ASCII', 'ignore').decode('utf-8')
    name = name.lower()
    name = re.sub(r'\b(jr|sr|ii|iii|iv)\b\.?', '', name)
    name = re.sub(r'[^a-z\s]', '', name)
    return ' '.join(name.split())


class NameResolver:
    """
    Maps raw player names to a canonical normalized name within one event.
    Exact normalized match first. Fuzzy only if: same last name, ratio >= FUZZY_MIN,
    and exactly one clear winner. Ambiguous names are skipped, never guessed.
    """
    def __init__(self):
        self.canon = []
        self.canon_set = set()

    def resolve(self, raw):
        n = normalize_name(raw)
        if not n:
            return None
        if n in self.canon_set:
            return n
        last = n.split()[-1]
        scored = []
        for c in self.canon:
            if c.split()[-1] != last:
                continue
            r = SequenceMatcher(None, n, c).ratio()
            if r >= FUZZY_MIN:
                scored.append((r, c))
        scored.sort(reverse=True)
        if len(scored) == 1:
            return scored[0][1]
        if len(scored) > 1:
            if scored[0][0] - scored[1][0] >= 0.03:
                return scored[0][1]
            return None  # ambiguous
        self.canon.append(n)
        self.canon_set.add(n)
        return n


# ----------------------------------------------------------------------------
# LOG / DEDUP / EXPOSURE
# ----------------------------------------------------------------------------
def dedup_key(game_date, game, market, player, side, line):
    return (
        str(game_date).strip(),
        str(game).strip().lower(),
        str(market).strip().lower(),
        normalize_name(player),
        str(side).strip().lower(),
        str(line).strip(),
    )


@contextmanager
def log_lock(timeout=60, stale=300):
    """Simple lock file shared with ev_grader_v2.py so the two never write the CSV at once."""
    start = time.time()
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK_PATH) > stale:
                    os.remove(LOCK_PATH)
                    continue
            except OSError:
                pass
            if time.time() - start > timeout:
                raise TimeoutError("Could not acquire log lock")
            time.sleep(0.5)
    try:
        yield
    finally:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass


def prepare_log():
    """
    - header == CSV_HEADER (with Result cols): nothing to do
    - header == BASE_HEADER (v2 log without Result cols): upgraded in place
    - anything else (v1 log): renamed to *_legacy_<ts>.csv, fresh log starts
    """
    if not os.path.isfile(CSV_FILENAME) or os.path.getsize(CSV_FILENAME) == 0:
        return
    with log_lock():
        with open(CSV_FILENAME, mode='r', newline='', encoding='utf-8') as f:
            data = list(csv.reader(f))
        if not data:
            return
        first = data[0]
        if first == CSV_HEADER:
            return
        if first == BASE_HEADER:
            tmp = CSV_FILENAME + '.tmp'
            with open(tmp, 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(CSV_HEADER)
                for r in data[1:]:
                    w.writerow(r[:len(BASE_HEADER)] + ['PENDING', '0.00'])
            os.replace(tmp, CSV_FILENAME)
            print("Log upgraded with Result / Net Units columns.")
            return
        legacy = CSV_FILENAME.replace('.csv', f'_legacy_{int(time.time())}.csv')
        os.rename(CSV_FILENAME, legacy)
        print(f"Old-format log renamed to {legacy}; starting a fresh log.")


def load_log():
    seen, game_exp, player_exp = set(), {}, {}
    if not os.path.isfile(CSV_FILENAME) or os.path.getsize(CSV_FILENAME) == 0:
        return seen, game_exp, player_exp
    with open(CSV_FILENAME, mode='r', newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            player = row.get('Player', '')
            game = row.get('Game', '')
            if not player or not game:
                continue
            date = row.get('Game Date', '')
            seen.add(dedup_key(date, game, row.get('Market', ''), player,
                               row.get('Side', ''), row.get('Line', '')))
            try:
                units = float(row.get('Kelly Units') or 0)
            except ValueError:
                units = 0.0
            if units > 0:
                gk = (date, game.strip().lower())
                pk = (date, normalize_name(player))
                game_exp[gk] = game_exp.get(gk, 0.0) + units
                player_exp[pk] = player_exp.get(pk, 0.0) + units
    return seen, game_exp, player_exp


def log_to_csv(plays, run_id, ts_str):
    with log_lock():
        is_empty = not os.path.isfile(CSV_FILENAME) or os.path.getsize(CSV_FILENAME) == 0
        with open(CSV_FILENAME, mode='a', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            if is_empty:
                w.writerow(CSV_HEADER)
            for p in plays:
                w.writerow([
                    run_id, ts_str, p['game_date'], p['sport'], p['game'], p['market'], p['player'],
                    p['side'], p['line'], p['book'], p['odds_str'],
                    f"{p['fair'] * 100:.1f}", f"{p['edge'] * 100:.2f}",
                    f"{p['units']:.2f}", f"{p['wager']:.2f}",
                    p['peers'], f"{p['spread'] * 100:.1f}", p['flag'],
                    'PENDING', '0.00'
                ])


# ----------------------------------------------------------------------------
# DISCORD
# ----------------------------------------------------------------------------
def format_block(p):
    if p['flag']:
        icon = "⚠️"
    elif p['edge'] >= 0.05:
        icon = "🔥"
    else:
        icon = "💎"
    stake = "VERIFY - no stake" if p['flag'] else f"{p['units']:.2f}u (${p['wager']:.2f})"
    l1 = f"{icon} **+{p['edge'] * 100:.2f}%** | **{p['player']}** {p['side']} {p['line']} {p['market']}"
    l2 = (f"↳ **{p['odds_str']}** @ {p['book']} • {stake} • "
          f"*Fair {prob_to_american(p['fair'])} ({p['peers']} books)*")
    l3 = f"  *{p['game']}*"
    return f"{l1}\n{l2}\n{l3}"


def send_discord_digest(plays, run_ts):
    if not DISCORD_WEBHOOK_URL or not plays:
        return
    plays = sorted(plays, key=lambda p: p['edge'], reverse=True)

    chunks, cur, cur_len = [], [], 0
    for block in (format_block(p) for p in plays):
        if cur and (len(cur) >= 15 or cur_len + len(block) + 2 > 3800):
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(block)
        cur_len += len(block) + 2
    if cur:
        chunks.append(cur)

    for i, chunk in enumerate(chunks, 1):
        part_tag = f" (Part {i}/{len(chunks)})" if len(chunks) > 1 else ""
        embed = {
            "title": f"🚨 +EV Prop Digest ({len(plays)} Plays){part_tag}",
            "description": "\n\n".join(chunk),
            "color": 65280,
            "footer": {"text": f"Scanned at {run_ts} CT • Power-devig consensus fair value"}
        }
        for _ in range(3):
            try:
                r = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
                if r.status_code == 429:
                    try:
                        wait = float(r.json().get('retry_after', 2))
                    except Exception:
                        wait = 2.0
                    time.sleep(wait + 0.5)
                    continue
                break
            except Exception as e:
                print(f"Error sending Discord digest: {e}")
                break
        time.sleep(1)


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def make_session():
    s = requests.Session()
    kwargs = dict(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  respect_retry_after_header=True)
    try:
        retry = Retry(allowed_methods=frozenset(['GET']), **kwargs)
    except TypeError:  # older urllib3
        retry = Retry(method_whitelist=frozenset(['GET']), **kwargs)
    s.mount('https://', HTTPAdapter(max_retries=retry))
    return s


# ----------------------------------------------------------------------------
# CORE: PARSE + FIND EDGES
# ----------------------------------------------------------------------------
def parse_event_books(event_data):
    """
    -> {market_key: {(player_norm, point): {book_key: {'Over': price, 'Under': price,
                                                        'title', 'updated', 'player_raw'}}}}
    FanDuel is processed first so its names become the canonical spellings.
    """
    resolver = NameResolver()
    data = {}
    books = sorted(event_data.get('bookmakers', []), key=lambda b: b.get('key') != 'fanduel')
    for book in books:
        bkey = book.get('key')
        if bkey not in ALLOWED_BOOKS:
            continue
        for market in book.get('markets', []):
            m_key = market.get('key')
            updated = parse_ts(market.get('last_update')) or parse_ts(book.get('last_update'))
            for o in market.get('outcomes', []):
                side, pt, price = o.get('name'), o.get('point'), o.get('price')
                if side not in ('Over', 'Under') or pt is None or price is None:
                    continue
                raw = o.get('description')
                player = resolver.resolve(raw)
                if not player:
                    continue
                slot = (data.setdefault(m_key, {})
                            .setdefault((player, pt), {})
                            .setdefault(bkey, {'title': book.get('title', bkey),
                                               'updated': updated,
                                               'player_raw': raw}))
                slot[side] = price
    return data


def find_event_candidates(event_data, ctx, now_utc):
    """Return the best-priced qualifying offer per (market, player, line, side)."""
    data = parse_event_books(event_data)
    best = {}

    for m_key, props in data.items():
        for (player, pt), by_book in props.items():
            # Devig every fresh two-sided book
            devigged = {}
            for bkey, s in by_book.items():
                if 'Over' in s and 'Under' in s and is_fresh(s['updated'], now_utc):
                    dv = devig_power(american_to_prob(s['Over']), american_to_prob(s['Under']))
                    if dv:
                        devigged[bkey] = {'Over': dv[0], 'Under': dv[1]}

            for target_key, s in by_book.items():
                if target_key in EXCLUDE_AS_TARGET or not is_fresh(s['updated'], now_utc):
                    continue
                peers = {k: v for k, v in devigged.items() if k != target_key}
                if len(peers) < MIN_FAIR_BOOKS:
                    continue

                for side in ('Over', 'Under'):
                    if side not in s:
                        continue
                    probs = [(BOOK_WEIGHTS.get(k, 1.0), v[side]) for k, v in peers.items()]
                    spread = max(p for _, p in probs) - min(p for _, p in probs)
                    if spread > MAX_PEER_SPREAD:
                        continue
                    fair = sum(w * p for w, p in probs) / sum(w for w, _ in probs)
                    dec = american_to_decimal(s[side])
                    edge = fair * dec - 1
                    if edge < MIN_EDGE:
                        continue

                    odds = s[side]
                    cand = {
                        'sport': ctx['sport'], 'game': ctx['game'], 'game_date': ctx['game_date'],
                        'market': format_market_name(m_key), 'player': s['player_raw'],
                        'side': side, 'line': pt, 'book': s['title'],
                        'odds_str': f"+{odds}" if odds > 0 else str(odds),
                        'dec': dec, 'fair': fair, 'edge': edge,
                        'peers': len(peers), 'spread': spread,
                    }
                    gk = (m_key, player, pt, side)
                    if gk not in best or edge > best[gk]['edge']:
                        best[gk] = cand
    return list(best.values())


def build_plays(candidates, seen, game_exp, player_exp):
    """Dedup, flag suspects, size stakes with exposure caps. Highest edge gets room first."""
    plays, capped = [], 0
    for c in sorted(candidates, key=lambda c: c['edge'], reverse=True):
        dk = dedup_key(c['game_date'], c['game'], c['market'], c['player'], c['side'], c['line'])
        if dk in seen:
            continue

        flag = 'SUSPECT' if c['edge'] >= SUSPECT_EDGE else ''
        units = 0.0
        if not flag:
            b = c['dec'] - 1
            kelly = (c['fair'] * b - (1 - c['fair'])) / b
            units = kelly * 100 * KELLY_FRACTION

            gk = (c['game_date'], c['game'].strip().lower())
            pk = (c['game_date'], normalize_name(c['player']))
            room = min(MAX_BET_UNITS,
                       MAX_GAME_UNITS - game_exp.get(gk, 0.0),
                       MAX_PLAYER_UNITS - player_exp.get(pk, 0.0))
            units = min(units, room)
            if units < MIN_BET_UNITS:
                capped += 1
                continue
            game_exp[gk] = game_exp.get(gk, 0.0) + units
            player_exp[pk] = player_exp.get(pk, 0.0) + units

        seen.add(dk)
        p = dict(c)
        p.update(units=units, wager=units * UNIT_SIZE, flag=flag)
        plays.append(p)
    return plays, capped


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def fetch_and_scan():
    if not API_KEY:
        print("CRITICAL ERROR: API Key missing.")
        return

    prepare_log()
    seen, game_exp, player_exp = load_log()
    session = make_session()

    utc_now = datetime.now(timezone.utc)
    c_now = to_central(utc_now)
    run_ts = c_now.strftime("%Y-%m-%d %H:%M:%S")
    run_id = c_now.strftime("%Y%m%d-%H%M%S")
    window_end = utc_now + timedelta(hours=LOOKAHEAD_HOURS)
    fmt = '%Y-%m-%dT%H:%M:%SZ'
    print(f"--- Starting EV Prop Scanner v2 (Run at {run_ts} CT, lookahead {LOOKAHEAD_HOURS:g}h) ---")

    all_candidates = []
    quota_remaining = None
    out_of_quota = False

    for sport, markets in SPORTS_CONFIG.items():
        if out_of_quota:
            break
        print(f"\nFetching schedule for {sport}...")
        try:
            events_res = session.get(
                f'https://api.the-odds-api.com/v4/sports/{sport}/events',
                params={'apiKey': API_KEY,
                        'commenceTimeFrom': utc_now.strftime(fmt),
                        'commenceTimeTo': window_end.strftime(fmt)},
                timeout=15)
        except Exception as e:
            print(f"Network error fetching events: {e}")
            continue
        if events_res.status_code != 200:
            print(f"API error fetching schedule for {sport}: {events_res.status_code}")
            continue

        for event in events_res.json():
            commence = parse_ts(event.get('commence_time'))
            if commence is None or commence <= utc_now or commence > window_end:
                continue  # unparseable, already started, or outside lookahead

            game_name = f"{event['away_team']} @ {event['home_team']}"
            game_date = to_central(commence).strftime('%Y-%m-%d')
            print(f"  -> Scanning {game_name}...")

            try:
                odds_res = session.get(
                    f"https://api.the-odds-api.com/v4/sports/{sport}/events/{event['id']}/odds",
                    params={'apiKey': API_KEY, 'markets': markets,
                            'bookmakers': KS_BOOKS, 'oddsFormat': 'american'},
                    timeout=15)
            except Exception as e:
                print(f"Network error on {game_name}: {e}")
                continue

            rem = odds_res.headers.get('x-requests-remaining')
            if rem is not None:
                try:
                    quota_remaining = float(rem)
                except ValueError:
                    pass
            if odds_res.status_code != 200:
                print(f"API error fetching odds for {game_name} ({sport}): {odds_res.status_code}")
                continue

            ctx = {'sport': sport, 'game': game_name, 'game_date': game_date}
            all_candidates.extend(
                find_event_candidates(odds_res.json(), ctx, datetime.now(timezone.utc)))

            if quota_remaining is not None and quota_remaining < MIN_QUOTA_REMAINING:
                print(f"Quota low ({quota_remaining:g} left). Stopping early.")
                out_of_quota = True
                break

    plays, capped = build_plays(all_candidates, seen, game_exp, player_exp)

    if plays:
        log_to_csv(plays, run_id, run_ts)
        send_discord_digest(plays, run_ts)

    suspects = sum(1 for p in plays if p['flag'])
    print(f"\nScan complete. {len(plays)} new plays logged/alerted "
          f"({suspects} flagged SUSPECT, {capped} skipped by exposure caps).")
    if quota_remaining is not None:
        print(f"API credits remaining: {quota_remaining:g}")


if __name__ == "__main__":
    fetch_and_scan()
