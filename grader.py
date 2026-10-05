"""
EV Auto-Grader v2  (companion to ev_prop_scanner_v2.py)

Changes vs v1:
  - Combo markets (PR / PRA) now SUM their components (v1 graded them as points only)
  - Each play is matched to its own game (teams + game date) instead of searching every
    recent box score; players matched by exact normalized name (strict fuzzy fallback)
  - DNP => VOID; missing/'--' stats are never treated as zero
  - Football: player present in the box score but absent from the target stat table = 0
  - Hockey points fall back to G + A if ESPN has no PTS label
  - SUSPECT / zero-unit plays are excluded from W-L and ROI (suspects get their own line)
  - Edge buckets match the scanner (<5%, 5-8%, 8%+)
  - Postponed/canceled games => VOID; plays still pending after MAX_PENDING_DAYS => UNMATCHED
  - Safe CSV handling: lock file + atomic replace, and results are applied to a FRESH read of
    the log so rows the scanner appended while grading are never lost
  - Failures are logged instead of silently swallowed
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
    CENTRAL = None

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')
CSV_FILENAME = 'ev_plays_log.csv'
LOCK_PATH = CSV_FILENAME + '.lock'
UNIT_SIZE = 25.00
MAX_PENDING_DAYS = 4
FUZZY_MIN = 0.92

ESPN_LEAGUES = {
    'basketball_wnba': ('basketball', 'wnba'),
    'basketball_nba': ('basketball', 'nba'),
    'basketball_nba_preseason': ('basketball', 'nba'),
    'icehockey_nhl': ('hockey', 'nhl'),
    'icehockey_nhl_preseason': ('hockey', 'nhl'),
    'americanfootball_nfl': ('football', 'nfl'),
    'americanfootball_ncaaf': ('football', 'college-football'),
}
VOID_STATUSES = {'STATUS_POSTPONED', 'STATUS_CANCELED', 'STATUS_CANCELLED'}

# market -> (stat table name or None, [component, ...]); each component is a list of
# alternative labels tried in order. A label can be a tuple, meaning "sum these labels".
# Components are SUMMED (that's how PR / PRA work). Pseudo-labels: PASS_ATT, 3PT_MADE.
MARKET_SPECS = {
    'Points': (None, [['PTS', 'P', ('G', 'A')]]),
    'Rebounds': (None, [['REB']]),
    'Assists': (None, [['AST', 'A']]),
    'Points Rebounds': (None, [['PTS'], ['REB']]),
    'Points Rebounds Assists': (None, [['PTS'], ['REB'], ['AST']]),
    'Threes': (None, [['3PT_MADE']]),
    'Shots On Goal': (None, [['SOG', 'S']]),
    'Total Saves': (None, [['SV', 'SAVES']]),
    'Pass Yds': ('passing', [['YDS']]),
    'Pass Attempts': ('passing', [['PASS_ATT']]),
    'Rush Yds': ('rushing', [['YDS']]),
    'Rush Attempts': ('rushing', [['CAR']]),
    'Reception Yds': ('receiving', [['YDS']]),
    'Receptions': ('receiving', [['REC']]),
}
FOOTBALL_MARKETS = {'Pass Yds', 'Pass Attempts', 'Rush Yds', 'Rush Attempts', 'Reception Yds', 'Receptions'}
HOCKEY_MARKETS = {'Shots On Goal', 'Total Saves'}

BUCKET_LABELS = ["1️⃣ **Under 5% Edge**", "2️⃣ **5.0% to 7.99% Edge**", "3️⃣ **8.0%+ Edge**"]


# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------
def normalize_name(name):
    if not name:
        return ""
    name = unicodedata.normalize('NFKD', str(name)).encode('ASCII', 'ignore').decode('utf-8')
    name = name.lower()
    name = re.sub(r'\b(jr|sr|ii|iii|iv)\b\.?', '', name)
    name = re.sub(r'[^a-z\s]', '', name)
    return ' '.join(name.split())


def american_to_decimal(odds):
    if odds > 0:
        return (odds / 100) + 1
    return (100 / abs(odds)) + 1


def _nth_sunday(year, month, n):
    d = datetime(year, month, 1)
    first_sunday = d + timedelta(days=(6 - d.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def to_central(dt_utc):
    if CENTRAL is not None:
        return dt_utc.astimezone(CENTRAL)
    y = dt_utc.year
    dst_start = _nth_sunday(y, 3, 2).replace(hour=8, tzinfo=timezone.utc)
    dst_end = _nth_sunday(y, 11, 1).replace(hour=7, tzinfo=timezone.utc)
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


def make_session():
    s = requests.Session()
    kwargs = dict(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                  respect_retry_after_header=True)
    try:
        retry = Retry(allowed_methods=frozenset(['GET']), **kwargs)
    except TypeError:
        retry = Retry(method_whitelist=frozenset(['GET']), **kwargs)
    s.mount('https://', HTTPAdapter(max_retries=retry))
    return s


# ----------------------------------------------------------------------------
# SAFE CSV ACCESS (shared lock convention with the scanner)
# ----------------------------------------------------------------------------
@contextmanager
def log_lock(timeout=60, stale=300):
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


def write_atomic(header, rows, as_dicts=False):
    tmp = CSV_FILENAME + '.tmp'
    with open(tmp, 'w', newline='', encoding='utf-8') as f:
        if as_dicts:
            w = csv.DictWriter(f, fieldnames=header, extrasaction='ignore', restval='')
            w.writeheader()
            w.writerows(rows)
        else:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    os.replace(tmp, CSV_FILENAME)


def migrate_log():
    """Make sure the log has Result / Net Units columns (older logs get them added)."""
    if not os.path.isfile(CSV_FILENAME) or os.path.getsize(CSV_FILENAME) == 0:
        return False
    with log_lock():
        with open(CSV_FILENAME, 'r', newline='', encoding='utf-8') as f:
            data = list(csv.reader(f))
        if not data:
            return False
        header = data[0]
        if 'Result' not in header:
            out = []
            for r in data[1:]:
                out.append(r + (['---', '---'] if r and r[0] == '---' else ['PENDING', '0.00']))
            write_atomic(header + ['Result', 'Net Units'], out)
    return True


def read_log():
    with open(CSV_FILENAME, 'r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        rows = []
        for row in reader:
            row.pop(None, None)
            rows.append(row)
    return header, rows


KEY_FIELDS = ('Run ID', 'Timestamp', 'Game', 'Market', 'Player', 'Side', 'Line', 'Bookmaker')


def row_key(row):
    return tuple(str(row.get(k, '') or '').strip() for k in KEY_FIELDS)


def is_real_row(row):
    player = row.get('Player', '') or ''
    game = row.get('Game', '') or ''
    return bool(player) and not player.startswith('---') and not game.startswith('===')


def is_pending(row):
    return is_real_row(row) and (row.get('Result') in (None, '', 'PENDING'))


def allowed_dates(row):
    """Central-time game dates an ESPN event may have to count as this play's game."""
    gd = (row.get('Game Date') or '').strip()
    if gd:
        return {gd}
    ts = (row.get('Timestamp') or '')[:10]  # older logs: logged date or the day after
    try:
        d = datetime.strptime(ts, '%Y-%m-%d')
    except ValueError:
        return set()
    return {ts, (d + timedelta(days=1)).strftime('%Y-%m-%d')}


# ----------------------------------------------------------------------------
# ESPN: SCOREBOARD, EVENT MATCHING, BOX SCORES
# ----------------------------------------------------------------------------
def team_matches(odds_name, team):
    n = normalize_name(odds_name)
    if not n or not team:
        return False
    cands = {normalize_name(team.get(k)) for k in ('displayName', 'shortDisplayName', 'name', 'location')}
    cands.discard('')
    if n in cands:
        return True
    for c in cands:
        if SequenceMatcher(None, n, c).ratio() >= 0.88:
            return True
    disp = normalize_name(team.get('displayName'))
    if disp and disp.split()[-1] == n.split()[-1] and SequenceMatcher(None, n, disp).ratio() >= 0.6:
        return True
    return False


def parse_event(ev):
    try:
        comp = ev['competitions'][0]
        teams = {c['homeAway']: c['team'] for c in comp['competitors']}
        st = ev['status']['type']
        dt = parse_ts(ev.get('date'))
        return {
            'id': str(ev['id']),
            'central_date': to_central(dt).strftime('%Y-%m-%d') if dt else None,
            'completed': bool(st.get('completed')),
            'status_name': st.get('name', ''),
            'home': teams.get('home', {}),
            'away': teams.get('away', {}),
        }
    except (KeyError, IndexError, TypeError):
        return None


def scoreboard_events(session, sport, league, yyyymmdd, cache):
    key = (sport, league, yyyymmdd)
    if key in cache:
        return cache[key]
    base = (f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/"
            f"scoreboard?dates={yyyymmdd}&limit=300")
    urls = [base] if league != 'college-football' else [f"{base}&groups=80", f"{base}&groups=81"]
    out = {}
    for url in urls:
        try:
            r = session.get(url, timeout=10)
            if r.status_code != 200:
                print(f"  ESPN scoreboard {league} {yyyymmdd}: HTTP {r.status_code}")
                continue
            for ev in r.json().get('events', []):
                parsed = parse_event(ev)
                if parsed:
                    out[parsed['id']] = parsed
        except Exception as e:
            print(f"  ESPN scoreboard error ({league} {yyyymmdd}): {e}")
    cache[key] = list(out.values())
    return cache[key]


def leagues_for_row(row):
    sport = (row.get('Sport') or '').strip()
    if sport in ESPN_LEAGUES:
        return [ESPN_LEAGUES[sport]]
    market = row.get('Market', '')
    if market in FOOTBALL_MARKETS:
        return [('football', 'nfl'), ('football', 'college-football')]
    if market in HOCKEY_MARKETS:
        return [('hockey', 'nhl')]
    return [('basketball', 'nba'), ('basketball', 'wnba')]


def find_event(row, session, sb_cache):
    away, _, home = (row.get('Game') or '').partition(' @ ')
    dates = allowed_dates(row)
    if not away or not home or not dates:
        return None
    query_days = set()
    for d in dates:
        base = datetime.strptime(d, '%Y-%m-%d')
        for delta in (-1, 0, 1):
            query_days.add((base + timedelta(days=delta)).strftime('%Y%m%d'))
    for sport, league in leagues_for_row(row):
        for day in sorted(query_days):
            for ev in scoreboard_events(session, sport, league, day, sb_cache):
                if ev['central_date'] not in dates:
                    continue
                if team_matches(away, ev['away']) and team_matches(home, ev['home']):
                    return sport, league, ev
    return None


def index_boxscore(box):
    """name -> {'played': bool, 'groups': {group_name: (labels, stats)}}"""
    idx = {}
    for team in box.get('players', []):
        for grp in team.get('statistics', []):
            gname = grp.get('name', '') or ''
            labels = grp.get('labels', []) or []
            for ath in grp.get('athletes', []):
                nm = normalize_name((ath.get('athlete') or {}).get('displayName', ''))
                if not nm:
                    continue
                entry = idx.setdefault(nm, {'played': False, 'groups': {}})
                stats = ath.get('stats') or []
                if stats and not ath.get('didNotPlay'):
                    entry['played'] = True
                    entry['groups'][gname] = (labels, stats)
    return idx


def get_index(session, sport, league, event_id, box_cache):
    key = (sport, league, event_id)
    if key in box_cache:
        return box_cache[key] or None
    url = (f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/"
           f"summary?event={event_id}")
    try:
        r = session.get(url, timeout=10)
        if r.status_code == 200:
            box = r.json().get('boxscore')
            if box:
                box_cache[key] = index_boxscore(box)
                return box_cache[key]
        else:
            print(f"  ESPN summary {event_id}: HTTP {r.status_code}")
    except Exception as e:
        print(f"  ESPN summary error ({event_id}): {e}")
    box_cache[key] = False
    return None


def lookup_player(idx, name):
    n = normalize_name(name)
    if not n:
        return None
    if n in idx:
        return idx[n]
    last = n.split()[-1]
    hits = []
    for cand in idx:
        if cand.split()[-1] != last:
            continue
        r = SequenceMatcher(None, n, cand).ratio()
        if r >= FUZZY_MIN:
            hits.append((r, cand))
    hits.sort(reverse=True)
    if len(hits) == 1 or (len(hits) > 1 and hits[0][0] - hits[1][0] >= 0.03):
        return idx[hits[0][1]]
    return None


def read_label(token, labels, stats):
    """Return a float, or None if the label is missing / blank / '--'. Never defaults to 0."""
    if isinstance(token, tuple):
        vals = [read_label(t, labels, stats) for t in token]
        return None if any(v is None for v in vals) else sum(vals)
    if token == 'PASS_ATT':
        label, part = 'C/ATT', 1
    elif token == '3PT_MADE':
        label, part = '3PT', 0
    else:
        label, part = token, None
    if label not in labels:
        return None
    i = labels.index(label)
    if i >= len(stats):
        return None
    raw = str(stats[i]).strip()
    if raw in ('', '--'):
        return None
    try:
        if part is not None:
            for d in ('/', '-'):
                if d in raw:
                    return float(raw.split(d)[part])
        return float(raw)
    except (ValueError, IndexError):
        return None


def grade_value(market, player, idx):
    """-> ('VOID', None) | ('VALUE', actual) | None (can't grade yet)"""
    spec = MARKET_SPECS.get(market)
    if not spec:
        return None
    table, parts = spec
    entry = lookup_player(idx, player)
    if entry is None:
        return None
    if not entry['played']:
        return 'VOID', None
    has_table = (table in entry['groups']) if table else True
    total = 0.0
    for alternatives in parts:
        val = None
        for gname, (labels, stats) in entry['groups'].items():
            if table and gname != table:
                continue
            for token in alternatives:
                val = read_label(token, labels, stats)
                if val is not None:
                    break
            if val is not None:
                break
        if val is None:
            if table and not has_table:
                val = 0.0  # football: on the box score but never listed in this table
            else:
                return None
        total += val
    return 'VALUE', total


# ----------------------------------------------------------------------------
# GRADING
# ----------------------------------------------------------------------------
def grade_pending_row(row, session, sb_cache, box_cache):
    """-> None (still pending) or (result, net_units, actual)"""
    try:
        side = row['Side'].strip().lower()
        line = float(row['Line'])
        odds = float(str(row['Odds']).replace('+', ''))
        units = float(row.get('Kelly Units') or 0)
    except (KeyError, ValueError, AttributeError):
        return None

    found = find_event(row, session, sb_cache)
    if not found:
        return None
    sport, league, ev = found
    if ev['status_name'] in VOID_STATUSES:
        return 'VOID', 0.0, None
    if not ev['completed']:
        return None
    idx = get_index(session, sport, league, ev['id'], box_cache)
    if not idx:
        return None
    graded = grade_value(row.get('Market', ''), row.get('Player', ''), idx)
    if not graded:
        return None
    kind, actual = graded
    if kind == 'VOID':
        return 'VOID', 0.0, None

    if actual == line:
        return 'PUSH', 0.0, actual
    if (side == 'over' and actual > line) or (side == 'under' and actual < line):
        return 'WIN', units * (american_to_decimal(odds) - 1), actual
    return 'LOSS', -units, actual


def new_bucket():
    return {'W': 0, 'L': 0, 'P': 0, 'Units': 0.0, 'Staked': 0.0}


def bucket_index(edge_pct):
    return 0 if edge_pct < 5.0 else (1 if edge_pct < 8.0 else 2)


def tally(rows, batch_keys):
    all_b = [new_bucket() for _ in range(3)]
    day_b = [new_bucket() for _ in range(3)]
    susp = {'W': 0, 'L': 0, 'P': 0}
    voids = 0
    for row in rows:
        if not is_real_row(row):
            continue
        res = row.get('Result')
        if res == 'VOID':
            voids += 1
            continue
        if res not in ('WIN', 'LOSS', 'PUSH'):
            continue
        letter = res[0]
        if (row.get('Flag') or '').strip().upper() == 'SUSPECT':
            susp[letter] += 1
            continue
        try:
            units = float(row.get('Kelly Units') or 0)
            net = float(row.get('Net Units') or 0)
            edge = float(row.get('Edge %') or 0)
        except ValueError:
            continue
        if units <= 0:
            continue
        b = bucket_index(edge)
        targets = [all_b[b]]
        if row_key(row) in batch_keys:
            targets.append(day_b[b])
        for t in targets:
            t[letter] += 1
            t['Units'] += net
            t['Staked'] += units
    return day_b, all_b, susp, voids


def send_digest(day_b, all_b, susp, graded_count, batch_voids, lifetime_voids):
    if not DISCORD_WEBHOOK_URL:
        return
    lines = []
    d_units = d_staked = a_units = a_staked = 0.0
    for i in range(3):
        d, a = day_b[i], all_b[i]
        d_units += d['Units']
        d_staked += d['Staked']
        a_units += a['Units']
        a_staked += a['Staked']
        d_bets, a_bets = d['W'] + d['L'], a['W'] + a['L']
        d_pct = d['W'] / d_bets * 100 if d_bets else 0.0
        a_pct = a['W'] / a_bets * 100 if a_bets else 0.0
        d_roi = d['Units'] / d['Staked'] * 100 if d['Staked'] > 0 else 0.0
        a_roi = a['Units'] / a['Staked'] * 100 if a['Staked'] > 0 else 0.0
        lines.append(BUCKET_LABELS[i])
        lines.append(f"**This batch:** {d['W']}-{d['L']}-{d['P']} ({d_pct:.1f}%) | {d['Units']:+.2f}u ({d_roi:+.1f}% ROI)")
        lines.append(f"**Lifetime:** {a['W']}-{a['L']}-{a['P']} ({a_pct:.1f}%) | {a['Units']:+.2f}u ({a_roi:+.1f}% ROI)\n")

    d_roi = d_units / d_staked * 100 if d_staked > 0 else 0.0
    a_roi = a_units / a_staked * 100 if a_staked > 0 else 0.0
    lines.append(f"💰 **Batch Profit:** {d_units:+.2f} Units (${d_units * UNIT_SIZE:+.2f}) | {d_roi:+.1f}% ROI")
    lines.append(f"🏦 **Lifetime Profit:** {a_units:+.2f} Units (${a_units * UNIT_SIZE:+.2f}) | {a_roi:+.1f}% ROI")
    if sum(susp.values()):
        lines.append(f"⚠️ **Suspect (unstaked) record:** {susp['W']}-{susp['L']}-{susp['P']}")
    if lifetime_voids:
        lines.append(f"↩️ **Voided (DNP/postponed):** {batch_voids} this batch, {lifetime_voids} lifetime")

    embed = {
        "title": f"📊 EV Auto-Grader Report ({graded_count} New Settlements)",
        "description": "\n".join(lines),
        "color": 3447003,
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
            print(f"Error sending Discord report: {e}")
            break


def run_grader():
    if not migrate_log():
        print("No CSV found to grade.")
        return

    _, rows = read_log()
    pending = [r for r in rows if is_pending(r)]
    if not pending:
        print("No pending plays to grade.")
        return
    print(f"{len(pending)} pending plays. Matching them to ESPN games...")

    session = make_session()
    sb_cache, box_cache = {}, {}
    updates = {}  # row_key -> (result, net_str)
    graded_count = batch_voids = 0
    today = to_central(datetime.now(timezone.utc)).date()

    for row in pending:
        outcome = grade_pending_row(row, session, sb_cache, box_cache)
        if outcome:
            res, net, actual = outcome
            updates[row_key(row)] = (res, f"{net:.2f}")
            if res == 'VOID':
                batch_voids += 1
                print(f"Voided: {row['Player']} {row['Market']} (DNP/postponed)")
            else:
                graded_count += 1
                print(f"Graded: {row['Player']} {row['Side']} {row['Line']} {row['Market']} "
                      f"-> Actual: {actual:g} ({res})")
            continue
        dates = allowed_dates(row)
        if dates:
            game_day = datetime.strptime(min(dates), '%Y-%m-%d').date()
            if (today - game_day).days > MAX_PENDING_DAYS:
                updates[row_key(row)] = ('UNMATCHED', '0.00')
                print(f"UNMATCHED (needs manual check): {row['Player']} {row['Market']} - {row.get('Game')}")

    if not updates:
        print("No new pending plays were ready to be graded.")
        return

    # Apply results to a FRESH read so rows appended by the scanner meanwhile are preserved
    with log_lock():
        header, fresh = read_log()
        for r in fresh:
            k = row_key(r)
            if k in updates and is_pending(r):
                r['Result'], r['Net Units'] = updates[k]
        write_atomic(header, fresh, as_dicts=True)

    if graded_count > 0:
        _, final_rows = read_log()
        batch_keys = {k for k, (res, _) in updates.items() if res in ('WIN', 'LOSS', 'PUSH')}
        day_b, all_b, susp, lifetime_voids = tally(final_rows, batch_keys)
        send_digest(day_b, all_b, susp, graded_count, batch_voids, lifetime_voids)
    print(f"Done. {graded_count} graded, {batch_voids} voided.")


if __name__ == "__main__":
    run_grader()
