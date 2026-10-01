import os
import re
import csv
import requests
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

API_KEY = os.environ.get('ODDS_API_KEY')
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')
UNIT_SIZE = 25.00

# Approved Kansas books
KS_BOOKS = 'fanduel,draftkings,betmgm,caesars,espnbet,novig'
ALLOWED_BOOKS = set(KS_BOOKS.split(','))
CSV_FILENAME = 'ev_plays_log.csv'

SPORTS_CONFIG = {
    'basketball_wnba': 'player_points,player_rebounds,player_assists,player_points_rebounds,player_points_rebounds_assists',
    'icehockey_nhl': 'player_points,player_assists,player_shots_on_goal,player_total_saves',
    'icehockey_nhl_preseason': 'player_points,player_assists,player_shots_on_goal,player_total_saves',
    'americanfootball_nfl': 'player_pass_yds,player_pass_attempts,player_rush_yds,player_rush_attempts,player_reception_yds,player_receptions',
    'americanfootball_ncaaf': 'player_pass_yds,player_pass_attempts,player_rush_yds,player_rush_attempts,player_reception_yds,player_receptions'
}

def american_to_prob(odds):
    if odds < 0: return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)

def american_to_decimal(odds):
    if odds > 0: return (odds / 100) + 1
    return (100 / abs(odds)) + 1

def format_market_name(market_key):
    return market_key.replace('player_', '').replace('_', ' ').title()

def normalize_name(name):
    if not name: return ""
    name = unicodedata.normalize('NFKD', str(name)).encode('ASCII', 'ignore').decode('utf-8')
    name = name.lower()
    name = re.sub(r'\b(jr|sr|ii|iii|iv)\b\.?', '', name)
    name = re.sub(r'[^a-z\s]', '', name)
    return ' '.join(name.split())

def match_player_name(target_name, fd_names):
    target_norm = normalize_name(target_name)
    target_parts = target_norm.split()
    best_match, best_score = None, 0.0

    for fd_name in fd_names:
        fd_norm = normalize_name(fd_name)
        if target_norm == fd_norm: return fd_name
            
        score = SequenceMatcher(None, target_norm, fd_norm).ratio()
        fd_parts = fd_norm.split()
        
        if len(target_parts) >= 2 and len(fd_parts) >= 2:
            if target_parts[-1] == fd_parts[-1] and target_parts[0][0] == fd_parts[0][0]:
                if score >= 0.75 and score > best_score:
                    best_score, best_match = score, fd_name
                    continue
                    
        if score >= 0.85 and score > best_score:
            best_score, best_match = score, fd_name

    return best_match

def load_seen_plays():
    seen = set()
    if not os.path.isfile(CSV_FILENAME): return seen
    with open(CSV_FILENAME, mode='r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            player = row.get('Player', '')
            game = row.get('Game', '')
            if not player or player.startswith('---') or game.startswith('==='):
                continue
                
            key = (
                game.strip().lower(),
                row.get('Market', '').strip().lower(),
                normalize_name(player),
                row.get('Side', '').strip().lower(),
                str(row.get('Line', '')).strip()
            )
            seen.add(key)
    return seen

def log_batch_to_csv(new_plays, run_timestamp):
    file_exists = os.path.isfile(CSV_FILENAME)
    is_empty = not file_exists or os.path.getsize(CSV_FILENAME) == 0

    with open(CSV_FILENAME, mode='a', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        
        if is_empty:
            writer.writerow(['Timestamp', 'Game', 'Market', 'Player', 'Side', 'Line', 'Bookmaker', 'Odds', 'True Prob %', 'Edge %', 'Kelly Units', 'Bet Amount'])
        else:
            writer.writerow([
                '---',
                f'=== RUN: {run_timestamp} ({len(new_plays)} PLAYS FOUND) ===',
                '---', '---', '---', '---', '---', '---', '---', '---', '---', '---'
            ])

        for play in new_plays:
            writer.writerow([
                play['timestamp'], play['game'], play['market'], 
                play['player'], play['side'], play['line'], 
                play['book'], play['odds'], play['true_prob'], 
                play['edge'], play['units'], play['wager']
            ])

def send_discord_digest(new_plays, run_timestamp):
    if not DISCORD_WEBHOOK_URL or not new_plays:
        return

    # Sort descending by edge so top edges appear first
    sorted_plays = sorted(new_plays, key=lambda x: float(x['edge']), reverse=True)

    # Chunk into groups of 15 to stay well under Discord embed limits
    chunk_size = 15
    for chunk_idx in range(0, len(sorted_plays), chunk_size):
        chunk = sorted_plays[chunk_idx:chunk_idx + chunk_size]
        
        lines = []
        for play in chunk:
            edge_val = float(play['edge'])
            icon = "🔥" if edge_val >= 5.0 else ("🟢" if edge_val >= 2.0 else "▫️")
            
            line_1 = f"{icon} **+{play['edge']}%** | **{play['player']}** {play['side']} {play['line']} {play['market']}"
            line_2 = f"↳ **{play['odds']}** @ {play['book']} • **{play['units']}u** (${play['wager']}) • *{play['game']}*"
            lines.append(f"{line_1}\n{line_2}")

        total_chunks = (len(sorted_plays) + chunk_size - 1) // chunk_size
        part_tag = f" (Part {chunk_idx // chunk_size + 1}/{total_chunks})" if total_chunks > 1 else ""

        embed = {
            "title": f"🚨 +EV Prop Digest ({len(sorted_plays)} Plays Found){part_tag}",
            "description": "\n\n".join(lines),
            "color": 65280,
            "footer": {"text": f"Scanned at {run_timestamp} CT • Ranked by Edge %"}
        }

        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        except Exception as e:
            print(f"Error sending Discord digest: {e}")

def fetch_and_scan():
    if not API_KEY:
        print("CRITICAL ERROR: API Key missing.")
        return
        
    seen_plays = load_seen_plays()
    new_plays_to_log = []
    edges_found = 0
    
    run_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"--- Starting EV Prop Scanner (Run at {run_timestamp}) ---")
    
    # Local Central Time filter
    utc_now = datetime.now(timezone.utc)
    central_time = utc_now - timedelta(hours=5)
    today = central_time.date()
    
    start_local = datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    end_local = datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=timezone(timedelta(hours=-5)))
    
    for sport, markets in SPORTS_CONFIG.items():
        print(f"\nFetching Schedule for {sport}...")
        
        events_url = f'https://api.the-odds-api.com/v4/sports/{sport}/events'
        events_params = {'apiKey': API_KEY}
        
        try:
            events_res = requests.get(events_url, params=events_params, timeout=15)
        except Exception as e:
            print(f"Network error fetching events: {e}")
            continue
            
        if events_res.status_code != 200:
            print(f"API Error fetching schedule for {sport}: {events_res.text}")
            continue
            
        events = events_res.json()
        
        for event in events:
            event_id = event['id']
            game_name = f"{event['away_team']} @ {event['home_team']}"
            
            try:
                commence_time = datetime.strptime(event['commence_time'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
                if not (start_local.astimezone(timezone.utc) <= commence_time <= end_local.astimezone(timezone.utc)):
                    continue
            except Exception:
                pass
            
            odds_url = f'https://api.the-odds-api.com/v4/sports/{sport}/events/{event_id}/odds'
            odds_params = {'apiKey': API_KEY, 'regions': 'us,us_ex', 'markets': markets, 'bookmakers': KS_BOOKS, 'oddsFormat': 'american'}
            
            try:
                odds_res = requests.get(odds_url, params=odds_params, timeout=15)
            except Exception as e: 
                print(f"Network error on {game_name}: {e}")
                continue
                
            if odds_res.status_code != 200: 
                print(f"API Error fetching odds for {game_name} ({sport}): {odds_res.text}")
                continue
                
            event_data = odds_res.json()
            
            fd_props = {}
            for book in event_data.get('bookmakers', []):
                if book['key'] not in ALLOWED_BOOKS: continue
                
                if book['key'] == 'fanduel':
                    for market in book.get('markets', []):
                        m_key = market['key']
                        if m_key not in fd_props: fd_props[m_key] = {}
                        for outcome in market['outcomes']:
                            player = outcome.get('description', 'Unknown')
                            side = outcome['name']
                            pt = outcome.get('point')
                            if pt is None: continue
                            key = (player, pt)
                            if key not in fd_props[m_key]: fd_props[m_key][key] = {}
                            fd_props[m_key][key][side] = outcome['price']
            
            true_probs = {}
            for m_key, props in fd_props.items():
                true_probs[m_key] = {}
                for (player, pt), sides in props.items():
                    if 'Over' in sides and 'Under' in sides:
                        p_over = american_to_prob(sides['Over'])
                        p_under = american_to_prob(sides['Under'])
                        true_probs[m_key][(player, pt)] = {'Over': p_over / (p_over + p_under), 'Under': p_under / (p_over + p_under)}

            for book in event_data.get('bookmakers', []):
                if book['key'] not in ALLOWED_BOOKS: continue
                if book['key'] == 'fanduel': continue
                book_name = book['title']
                
                for market in book.get('markets', []):
                    m_key = market['key']
                    if m_key not in true_probs: continue
                    available_fd_lines = true_probs[m_key]
                    
                    for outcome in market['outcomes']:
                        raw_player = outcome.get('description', 'Unknown')
                        side = outcome['name']
                        pt = outcome.get('point')
                        avail_odds = outcome['price']
                        if pt is None: continue

                        candidate_fd_players = [p for (p, l) in available_fd_lines.keys() if l == pt]
                        matched_fd_player = match_player_name(raw_player, candidate_fd_players)
                        if not matched_fd_player: continue

                        line_key = (matched_fd_player, pt)
                        if side not in available_fd_lines[line_key]: continue

                        true_prob = available_fd_lines[line_key][side]
                        dec_odds = american_to_decimal(avail_odds)
                        edge = (true_prob * dec_odds) - 1
                        
                        if edge > 0:
                            edges_found += 1
                            m_display = format_market_name(m_key)
                            formatted_odds = f"+{avail_odds}" if avail_odds > 0 else str(avail_odds)
                            
                            dedup_key = (
                                game_name.strip().lower(), 
                                m_display.strip().lower(), 
                                normalize_name(raw_player), 
                                side.strip().lower(), 
                                str(pt).strip()
                            )
                            if dedup_key in seen_plays: continue

                            b = dec_odds - 1
                            kelly_decimal = (true_prob * b - (1 - true_prob)) / b
                            
                            kelly_units = kelly_decimal * 100
                            half_kelly_units = kelly_units / 2
                            dollar_wager = half_kelly_units * UNIT_SIZE
                            
                            play_data = {
                                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                'game': game_name, 'market': m_display, 'player': raw_player,
                                'side': side, 'line': pt, 'book': book_name, 'odds': formatted_odds,
                                'true_prob': f"{true_prob * 100:.1f}", 'edge': f"{edge * 100:.2f}",
                                'units': f"{half_kelly_units:.2f}", 'wager': f"{dollar_wager:.2f}"
                            }
                            
                            seen_plays.add(dedup_key)
                            new_plays_to_log.append(play_data)

    if new_plays_to_log:
        log_batch_to_csv(new_plays_to_log, run_timestamp)
        send_discord_digest(new_plays_to_log, run_timestamp)

    print(f"Scan complete. Found {edges_found} active edges ({len(new_plays_to_log)} new plays logged & alerted).")

if __name__ == "__main__":
    fetch_and_scan()
