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
    load_rows, match_name, meets_candidate, meets_rule, normalize_name, parse_american, parse_iso,
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