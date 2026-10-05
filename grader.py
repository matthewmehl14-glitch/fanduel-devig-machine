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
        ('basketball', 'nba'),
        ('hockey', 'nhl'), 
        ('football', 'nfl'),
        ('football', 'college-football')
    ]
    
    dates_to_check = [(datetime.now() - timedelta(days=i)).strftime('%Y%m%d') for i in range(3)]
    
    for sport, league in sports:
        for d in set(dates_to_check):
            base_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates={d}&limit=300"
            urls_to_check = [base_url]
            if league == 'college-football':
                urls_to_check = [f"{base_url}&groups=80", f"{base_url}&groups=81"]
                
            for url in urls_to_check:
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
    try:
        if stat_name == 'Pass Attempts' and 'C/ATT' in labels:
            val = stats[labels.index('C/ATT')]
            if val == '--': return 0.0
            delim = '/' if '/' in val else ('-' if '-' in val else None)
            return float(val.split(delim)[1]) if delim else float(val)

        if stat_name == '3PT Made' and '3PT' in labels:
            val = stats[labels.index('3PT')]
            if val == '--': return 0.0
            delim = '-' if '-' in val else ('/' if '/' in val else None)
            return float(val.split(delim)[0]) if delim else float(val)

        if stat_name in labels:
            val = stats[labels.index(stat_name)]
            return float(val) if val != '--' else 0.0
    except Exception:
        return None
    return None

def get_player_stat(boxscores, player_name, market):
    market_map = {
        'Points': [('PTS', None), ('P', 'skaters')],
        'Rebounds': [('REB', None)],
        'Assists': [('AST', None), ('A', 'skaters')],
        'Points Rebounds': [('PTS', None), ('REB', None)],
        'Points Rebounds Assists': [('PTS', None), ('REB', None), ('AST', None)],
        'Threes': [('3PT Made', None)],
        'Shots On Goal': [('SOG', 'skaters'), ('S', 'skaters')],
        'Total Saves': [('SV', 'goalies'), ('SAVES', 'goalies')],
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

def send_digest(daily_buckets, all_time_buckets, graded_count):
    if not DISCORD_WEBHOOK_URL: return
    
    labels = ["1️⃣ **0.0% to 1.99% Edge**", "2️⃣ **2.0% to 4.99% Edge**", "3️⃣ **5.0%+ Edge**"]
    
    daily_total_units = 0.0
    daily_total_staked = 0.0
    all_time_total_units = 0.0
    all_time_total_staked = 0.0
    
    lines = []
    
    for i in range(3):
        dw, dl, dp = daily_buckets[i]['W'], daily_buckets[i]['L'], daily_buckets[i]['P']
        d_units = daily_buckets[i]['Units']
        d_staked = daily_buckets[i]['Staked']
        daily_total_units += d_units
        daily_total_staked += d_staked
        d_bets = dw + dl
        d_pct = (dw / d_bets * 100) if d_bets > 0 else 0.0
        d_roi = (d_units / d_staked * 100) if d_staked > 0 else 0.0
        
        aw, al, ap = all_time_buckets[i]['W'], all_time_buckets[i]['L'], all_time_buckets[i]['P']
        a_units = all_time_buckets[i]['Units']
        a_staked = all_time_buckets[i]['Staked']
        all_time_total_units += a_units
        all_time_total_staked += a_staked
        a_bets = aw + al
        a_pct = (aw / a_bets * 100) if a_bets > 0 else 0.0
        a_roi = (a_units / a_staked * 100) if a_staked > 0 else 0.0
        
        lines.append(f"{labels[i]}")
        lines.append(f"**Today:** {dw}-{dl}-{dp} ({d_pct:.1f}%) | {d_units:+.2f}u ({d_roi:+.1f}% ROI)")
        lines.append(f"**Lifetime:** {aw}-{al}-{ap} ({a_pct:.1f}%) | {a_units:+.2f}u ({a_roi:+.1f}% ROI)\n")

    total_d_roi = (daily_total_units / daily_total_staked * 100) if daily_total_staked > 0 else 0.0
    total_a_roi = (all_time_total_units / all_time_total_staked * 100) if all_time_total_staked > 0 else 0.0

    lines.append(f"💰 **Batch Profit:** {daily_total_units:+.2f} Units (${daily_total_units * UNIT_SIZE:+.2f}) | {total_d_roi:+.1f}% ROI")
    lines.append(f"🏦 **Lifetime Profit:** {all_time_total_units:+.2f} Units (${all_time_total_units * UNIT_SIZE:+.2f}) | {total_a_roi:+.1f}% ROI")

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
    # Added 'Staked' tracking for ROI calculations
    daily_buckets = [{'W': 0, 'L': 0, 'P': 0, 'Units': 0.0, 'Staked': 0.0} for _ in range(3)]
    all_time_buckets = [{'W': 0, 'L': 0, 'P': 0, 'Units': 0.0, 'Staked': 0.0} for _ in range(3)]
    
    for row in rows:
        player_cell = row.get('Player', '')
        game_cell = row.get('Game', '')
        if not player_cell or player_cell.startswith('---') or game_cell.startswith('==='):
            continue
            
        edge = float(row.get('Edge %', 0))
        b_idx = 0 if edge < 2.0 else (1 if edge < 5.0 else 2)
        just_graded_now = False
        
        current_result = row.get('Result')
        if current_result in ['PENDING', None, '']:
            player = row['Player']
            market = row['Market']
            side = row['Side'].lower()
            line = float(row['Line'])
            odds = float(str(row['Odds']).replace('+', ''))
            units = float(row['Kelly Units'])
            
            actual = get_player_stat(boxscores, player, market)
            
            if actual is not None:
                newly_graded += 1
                just_graded_now = True
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
            else:
                row['Result'] = 'PENDING'
                row['Net Units'] = '0.00'

        if row.get('Result') in ['WIN', 'LOSS', 'PUSH']:
            res = row['Result']
            try:
                net = float(row.get('Net Units', 0))
            except (ValueError, TypeError):
                net = 0.0
                
            try:
                staked = float(row.get('Kelly Units', 0))
            except (ValueError, TypeError):
                staked = 0.0
            
            all_time_buckets[b_idx][res[0]] += 1
            all_time_buckets[b_idx]['Units'] += net
            all_time_buckets[b_idx]['Staked'] += staked
            
            if just_graded_now:
                daily_buckets[b_idx][res[0]] += 1
                daily_buckets[b_idx]['Units'] += net
                daily_buckets[b_idx]['Staked'] += staked

    if newly_graded > 0:
        fieldnames = list(rows[0].keys())
        if 'Result' not in fieldnames:
            fieldnames.extend(['Result', 'Net Units'])
            
        with open(CSV_FILENAME, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            
        send_digest(daily_buckets, all_time_buckets, newly_graded)
    else:
        print("No new pending plays were ready to be graded.")

if __name__ == "__main__":
    run_grader()
