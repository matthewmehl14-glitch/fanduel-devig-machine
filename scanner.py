import os
import re
import csv
import requests
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher

# Setup
API_KEY = os.environ.get('ODDS_API_KEY')
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')
UNIT_SIZE = 25.00
KS_BOOKS = 'fanduel,draftkings,betmgm,caesars,espnbet,novig'
CSV_FILENAME = 'ev_plays_log.csv'

SPORTS_CONFIG = {
    'basketball_wnba': 'player_points,player_rebounds,player_assists,player_points_rebounds,player_points_rebounds_assists',
    'icehockey_nhl': 'player_points,player_shots_on_goal,player_saves'
}

def american_to_prob(odds):
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)

def american_to_decimal(odds):
    if odds > 0:
        return (odds / 100) + 1
    return (100 / abs(odds)) + 1

def format_market_name(market_key):
    return market_key.replace('player_', '').replace('_', ' ').title()

# --- Fuzzy Name Matching Helpers ---
def normalize_name(name):
    if not name:
        return ""
    # Strip accents / diacritics
    name = unicodedata.normalize('NFKD', str(name)).encode('ASCII', 'ignore').decode('utf-8')
    name = name.lower()
    # Strip common suffixes
    name = re.sub(r'\b(jr|sr|ii|iii|iv)\b\.?', '', name)
    # Remove all non-letter characters
    name = re.sub(r'[^a-z\s]', '', name)
    return ' '.join(name.split())

def match_player_name(target_name, fd_names):
    """
    Matches target_name against a list of FanDuel player names using fuzzy ratio.
    Requires >= 0.85 similarity, or >= 0.75 if last name + first initial match.
    """
    target_norm = normalize_name(target_name)
    target_parts = target_norm.split()
    
    best_match = None
    best_score = 0.0

    for fd_name in fd_names:
        fd_norm = normalize_name(fd_name)
        if target_norm == fd_norm:
            return fd_name
            
        fd_parts = fd_norm.split()
        score = SequenceMatcher(None, target_norm, fd_norm).ratio()
        
        # Check first initial + last name match (e.g., Alex Ovechkin vs Alexander Ovechkin)
        if len(target_parts) >= 2 and len(fd_parts) >= 2:
            if target_parts[-1] == fd_parts[-1] and target_parts[0][0] == fd_parts[0][0]:
                if score >= 0.75 and score > best_score:
                    best_score = score
                    best_match = fd_name
                    continue
                    
        if score >= 0.85 and score > best_score:
            best_score = score
            best_match = fd_name

    return best_match

# --- Deduplication & Logging ---
def load_seen_plays():
    seen = set()
    if not os.path.isfile(CSV_FILENAME):
        return seen

    with open(CSV_FILENAME, mode='r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Build unique key: (game, market, normalized_player, side, line, book)
            key = (
                row.get('Game', '').strip().lower(),
                row.get('Market', '').strip().lower(),
                normalize_name(row.get('Player', '')),
                row.get('Side', '').strip().lower(),
                str(row.get('Line', '')).strip(),
                row.get('Bookmaker', '').strip().lower()
            )
            seen.add(key)
    return seen

def log_to_csv(play_data):
    file_exists = os.path.isfile(CSV_FILENAME)
    with open(CSV_FILENAME, mode='a', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(['Timestamp', 'Game', 'Market', 'Player', 'Side', 'Line', 'Bookmaker', 'Odds', 'True Prob %', 'Edge %', 'Kelly Units', 'Bet Amount'])
        
        writer.writerow([
            play_data['timestamp'], play_data['game'], play_data['market'], 
            play_data['player'], play_data['side'], play_data['line'], 
            play_data['book'], play_data['odds'], play_data['true_prob'], 
            play_data['edge'], play_data['units'], play_data['wager']
        ])

def send_discord_alert(play_data):
    if not DISCORD_WEBHOOK_URL:
        return
        
    embed = {
        "title": "🚨 +EV Play Detected",
        "color": 65280, # Neon Green
        "fields": [
            {"name": "Game", "value": play_data['game'], "inline": False},
            {"name": "Play", "value": f"{play_data['player']} {play_data['side']} {play_data['line']} {play_data['market']}", "inline": False},
            {"name": "Odds & Book", "value": f"{play_data['odds']} at **{play_data['book']}**", "inline": True},
            {"name": "Edge", "value": f"+{play_data['edge']}%", "inline": True},
            {"name": "True Prob", "value": f"{play_data['true_prob']}%", "inline": True},
            {"name": "Recommendation", "value": f"{play_data['units']} Units (${play_data['wager']})", "inline": False}
        ],
        "footer": {"text": f"Found at {play_data['timestamp']}"}
    }
    
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        print(f"Failed to send Discord alert: {e}")

# --- Core Scanner Engine ---
def fetch_and_scan():
    seen_plays = load_seen_plays()
    edges_found = 0
    new_alerts = 0
    
    print(f"--- Starting EV Prop Scanner ---")
    print(f"Loaded {len(seen_plays)} historical plays to prevent duplicate alerts.\n")
    
    for sport, markets in SPORTS_CONFIG.items():
        print(f"Scanning {sport.split('_')[1].upper()} Props...")
        
        url = f'https://api.the-odds-api.com/v4/sports/{sport}/odds/'
        params = {
            'apiKey': API_KEY,
            'regions': 'us,us_ex',
            'markets': markets,
            'bookmakers': KS_BOOKS,
            'oddsFormat': 'american'
        }
        
        try:
            response = requests.get(url, params=params, timeout=15)
        except Exception as e:
            print(f"Request timeout on {sport}: {e}")
            continue

        if response.status_code != 200:
            print(f"API Error on {sport}: {response.text}")
            continue
            
        events = response.json()
        
        for event in events:
            game_name = f"{event['away_team']} @ {event['home_team']}"
            fd_props = {}
            
            # Step 1: Extract FanDuel's props
            for book in event['bookmakers']:
                if book['key'] == 'fanduel':
                    for market in book.get('markets', []):
                        m_key = market['key']
                        if m_key not in fd_props:
                            fd_props[m_key] = {}
                            
                        for outcome in market['outcomes']:
                            player = outcome.get('description', 'Unknown')
                            side = outcome['name']
                            pt = outcome.get('point')
                            if pt is None:
                                continue
                                
                            key = (player, pt)
                            if key not in fd_props[m_key]:
                                fd_props[m_key][key] = {}
                            fd_props[m_key][key][side] = outcome['price']
            
            # Step 2: Multiplicative Devig on FanDuel
            true_probs = {}
            for m_key, props in fd_props.items():
                true_probs[m_key] = {}
                for (player, pt), sides in props.items():
                    if 'Over' in sides and 'Under' in sides:
                        p_over = american_to_prob(sides['Over'])
                        p_under = american_to_prob(sides['Under'])
                        total_implied = p_over + p_under
                        true_probs[m_key][(player, pt)] = {
                            'Over': p_over / total_implied,
                            'Under': p_under / total_implied
                        }

            # Step 3: Compare other books against FanDuel
            for book in event['bookmakers']:
                if book['key'] == 'fanduel':
                    continue
                book_name = book['title']
                
                for market in book.get('markets', []):
                    m_key = market['key']
                    if m_key not in true_probs:
                        continue
                        
                    # Pre-gather all FD player names available at this line
                    available_fd_lines = true_probs[m_key]
                    
                    for outcome in market['outcomes']:
                        raw_player = outcome.get('description', 'Unknown')
                        side = outcome['name']
                        pt = outcome.get('point')
                        avail_odds = outcome['price']
                        
                        if pt is None:
                            continue

                        # Find FD players available at this exact market and point
                        candidate_fd_players = [p for (p, l) in available_fd_lines.keys() if l == pt]
                        matched_fd_player = match_player_name(raw_player, candidate_fd_players)
                        
                        if not matched_fd_player:
                            continue

                        line_key = (matched_fd_player, pt)
                        if side not in available_fd_lines[line_key]:
                            continue

                        true_prob = available_fd_lines[line_key][side]
                        dec_odds = american_to_decimal(avail_odds)
                        edge = (true_prob * dec_odds) - 1
                        
                        if edge > 0:
                            edges_found += 1
                            m_display = format_market_name(m_key)
                            
                            # Construct deduplication key
                            dedup_key = (
                                game_name.strip().lower(),
                                m_display.strip().lower(),
                                normalize_name(raw_player),
                                side.strip().lower(),
                                str(pt).strip(),
                                book_name.strip().lower()
                            )
                            
                            # Skip if already logged in a prior run
                            if dedup_key in seen_plays:
                                continue

                            # Calculation
                            b = dec_odds - 1
                            kelly = (true_prob * b - (1 - true_prob)) / b
                            half_kelly_units = kelly / 2
                            dollar_wager = half_kelly_units * UNIT_SIZE
                            formatted_odds = f"+{avail_odds}" if avail_odds > 0 else str(avail_odds)
                            
                            play_data = {
                                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                'game': game_name,
                                'market': m_display,
                                'player': raw_player,
                                'side': side,
                                'line': pt,
                                'book': book_name,
                                'odds': formatted_odds,
                                'true_prob': f"{true_prob * 100:.1f}",
                                'edge': f"{edge * 100:.2f}",
                                'units': f"{half_kelly_units:.2f}",
                                'wager': f"{dollar_wager:.2f}"
                            }
                            
                            print(f"[NEW EDGE] [{game_name}] {raw_player} {side} {pt} {m_display}")
                            print(f"Odds: {formatted_odds} at {book_name} | Edge: +{play_data['edge']}% | Rec: {play_data['units']}u (${play_data['wager']})\n")
                            
                            # Add to tracking set, write to CSV, and notify Discord
                            seen_plays.add(dedup_key)
                            log_to_csv(play_data)
                            send_discord_alert(play_data)
                            new_alerts += 1

    print(f"Scan complete. Found {edges_found} active edges ({new_alerts} new alerts triggered).")

if __name__ == "__main__":
    fetch_and_scan()
