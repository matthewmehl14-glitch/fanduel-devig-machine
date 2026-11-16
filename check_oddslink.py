"""One-time check: does OddsLink carry the books and NBA/NHL props the scanner needs?
Uses only free listing calls (no allowance used). Key comes from the ODDSLINK_API_KEY secret."""
import json
import os

import requests

KEY = os.environ.get("ODDSLINK_API_KEY")
BASE = "https://oddslink.app/v1"
MY_BOOKS = ["novig", "fanduel", "draftkings", "betmgm", "caesars", "thescore"]


def get(path, **params):
    r = requests.get(f"{BASE}{path}", headers={"X-API-Key": KEY}, params=params, timeout=20)
    print(f"GET {path} {params or ''} -> {r.status_code}")
    r.raise_for_status()
    return r.json()


if not KEY:
    raise SystemExit("ODDSLINK_API_KEY secret is missing.")

books = get("/bookmakers")
books = books.get("data", books) if isinstance(books, dict) else books
print("\n=== YOUR BOOKS ===")
by_key = {b.get("key"): b for b in books}
for k in MY_BOOKS:
    b = by_key.get(k)
    if not b:
        print(f"  {k:11} NOT LISTED")
        continue
    print(f"  {k:11} props={b.get('has_player_props')}  prop sports={b.get('player_props_sports', 'not stated')}  "
          f"sports={b.get('sports')}")

for sport in ("nba", "nhl", "nfl"):
    mk = get("/markets", sport=sport)
    mk = mk.get("data", mk) if isinstance(mk, dict) else mk
    props = [m for m in mk if m.get("kind") == "player_prop"]
    print(f"\n=== {sport.upper()} PLAYER PROP MARKETS ({len(props)}) ===")
    for m in props:
        have = m.get("bookmakers")
        if have is None:
            tag = "books not stated"
        else:
            tag = " ".join(f"{k}{'✓' if k in have else '✗'}" for k in MY_BOOKS)
        print(f"  {m.get('key'):38} {m.get('description', '')[:30]:30} {tag}")

print("\n=== RAW BOOK LIST (for Claude) ===")
print(json.dumps([by_key[k] for k in MY_BOOKS if k in by_key], indent=1))
