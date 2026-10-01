import os
import re
import csv
import requests
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher

DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')
CSV_FILENAME = 'ev_plays_log.csv'
UNIT_SIZE = 25.00

def american_to_decimal(odds):
    if odds > 0: return (odds / 100) + 1
    return (100 / abs(odds)) + 1

def normalize_name(name):
    if not name: return ""
    name = unicodedata.normalize('NFKD', str(name)).encode('ASCII', 'ignore').decode('utf-8')
    name = name.lower()
    name = re.sub(r'\b(jr|sr|ii|iii|iv)\b\.?', '', name)
    name = re.sub(r'[^a-z\s]', '', name)
    return ' '.join(name.split())

def similarity_score(s1, s2):
    return SequenceMatcher(None, normalize_name(s1), normalize_name(s2)).ratio()

def migrate_csv():
    if not os.path.isfile(CSV_FILENAME): return False
    with open(CSV_FILENAME, 'r', encoding='utf-8') as f:
        reader = list(csv.reader(f))
    
    if not reader: return False
    headers = reader[0]
    
    if 'Result' not in headers:
        headers.extend(['Result', 'Net Units'])
        with open(CSV_FILENAME, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in reader[1:]:
                if row[0] == '---':
                    row.extend(['---', '---'])
                else:
                    row.extend(['PENDING', '0.00'])
                writer.writerow(row)
    return True

def fetch_recent_boxscores():
    print("Fetching ESPN box scores from the last 3 days...")
    boxscores = []
    seen_events = set()
    
    sports = [
        ('basketball', 'wnba'), 
        ('hockey', 'nhl'), 
        ('football', 'nfl'),
        ('football', 'college-football')
    ]
    
    dates_to_check = [(datetime.now() - timedelta(days=i)).strftime('%Y%m%d') for i in range(3)]
    
    for sport, league in sports:
        for d in set(dates_to_check):
            url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates={d}"
            try:
                res = requests.get(url, timeout=10)
                if res.status_code != 200: continue
                events = res.json().get('events', [])
                for event in events:
                    game_id = event['id']
                    if game_id in seen_events: continue
                    seen_events.add(game_id)
                    
                    if event['status']['type']['completed']:
                        summary_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary?event={game_id}"
                        sum_res = requests.get(summary_url, timeout=10)
                        if sum_res.status_code == 200:
                            box = sum_res.json().get('boxscore')
                            if box: boxscores.append(box)
            except Exception:
                pass
    return boxscores

def extract_stat_value(stat_name, labels, stats):
    if stat_name == 'Pass Attempts' and 'C/ATT' in labels:
        return float(stats[labels.index('C/ATT')].split('/')[1])
    if stat_name in labels:
        val = stats[labels.index(stat_name)]
        return float(val) if val != '--' else 0.0
    return None

def get_player_stat(boxscores, player_name, market):
    market_map = {
        'Points': [('PTS', None)],
        'Rebounds': [('REB', None)],
        'Assists': [('AST', None), ('A', 'skaters')],
        'Points Rebounds': [('PTS', None), ('REB', None)],
        'Points Rebounds Assists': [('PTS', None), ('REB', None), ('AST', None)],
        'Shots On Goal': [('SOG', 'skaters')],
        'Total Saves': [('SV', 'goalies')],
        'Pass Yds': [('YDS', 'passing')],
        'Pass Attempts': [('Pass Attempts', 'passing')],
        'Rush Yds': [('YDS', 'rushing')],
        'Rush Attempts': [('CAR', 'rushing')],
        'Reception Yds': [('YDS', 'receiving')],
        'Receptions': [('REC', 'receiving')]
    }
    
    targets = market_map.get(market)
    if not targets: return None
    
    for boxscore in boxscores:
        for target_stat, target_table in targets:
            stat_found = False
            total = 0.0
            for team in boxscore.get('players', []):
                for stat_group in team.get('statistics', []):
                    table_name = stat_group.get('name', '')
                    if target_table and target_table != table_name: continue
                        
                    labels = stat_group.get('labels', [])
                    for ath in stat_group.get('athletes', []):
                        ath_name = ath.get('athlete', {}).get('displayName', '')
                        if similarity_score(player_name, ath_name) > 0.85:
                            val = extract_stat_value(target_stat, labels, ath.get('stats', []))
                            if val is not None:
                                total += val
                                stat_found = True
                                break
                    if stat_found: break
                if stat_found: break
            if stat_found: 
                return total
    return None

def send_digest(buckets, graded_count):
    if not DISCORD_WEBHOOK_URL: return
    
    lines = []
    total_units = 0.0
    
    labels = ["1️⃣ **0.0% to 1.99% Edge**", "2️⃣ **2.0% to 4.99% Edge**", "3️⃣ **5.0%+ Edge**"]
    
    for i, b in enumerate(buckets):
        w, l, p, units = b['W'], b['L'], b['P'], b['Units']
        total_units += units
        total_bets = w + l
        win_pct = (w / total_bets * 100) if total_bets > 0 else 0.0
        
        lines.append(f"{labels[i]}")
        lines.append(f"Record: {w}-{l}-{p} ({win_pct:.1f}%) | {units:+.2f} Units\n")

    lines.append(f"💰 **Total System Profit:** {total_units:+.2f} Units (${total_units * UNIT_SIZE:+.2f})")

    embed = {
        "title": f"📊 EV Auto-Grader Report ({graded_count} New Settlements)",
        "description": "\n".join(lines),
        "color": 3447003
    }
    try: requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
    except Exception: pass

def run_grader():
    if not migrate_csv():
        print("No CSV found to grade.")
        return
        
    boxscores = fetch_recent_boxscores()
    print(f"Loaded {len(boxscores)} completed box scores.")
    
    with open(CSV_FILENAME, 'r', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
        
    newly_graded = 0
    buckets = [{'W': 0, 'L': 0, 'P': 0, 'Units': 0.0} for _ in range(3)]
    
    for row in rows:
        if row.get('Player', '').startswith('---'): continue
            
        edge = float(row.get('Edge %', 0))
        b_idx = 0 if edge < 2.0 else (1 if edge < 5.0 else 2)
        
        if row.get('Result') == 'PENDING':
            player = row['Player']
            market = row['Market']
            side = row['Side'].lower()
            line = float(row['Line'])
            odds = float(row['Odds'].replace('+', ''))
            units = float(row['Kelly Units'])
            
            actual = get_player_stat(boxscores, player, market)
            
            if actual is not None:
                newly_graded += 1
                if actual == line:
                    res = 'PUSH'
                    net = 0.0
                elif (side == 'over' and actual > line) or (side == 'under' and actual < line):
                    res = 'WIN'
                    net = units * (american_to_decimal(odds) - 1)
                else:
                    res = 'LOSS'
                    net = -units
                    
                row['Result'] = res
                row['Net Units'] = f"{net:.2f}"
                print(f"Graded: {player} {side} {line} {market} -> Actual: {actual} ({res})")

        # Compile bucket statistics for ALL graded plays (historical + new)
        if row.get('Result') in ['WIN', 'LOSS', 'PUSH']:
            res = row['Result']
            net = float(row['Net Units'])
            buckets[b_idx][res[0]] += 1
            buckets[b_idx]['Units'] += net

    if newly_graded > 0:
        fieldnames = list(rows[0].keys())
        with open(CSV_FILENAME, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            
        send_digest(buckets, newly_graded)
    else:
        print("No new pending plays were ready to be graded.")

if __name__ == "__main__":
    run_grader()
