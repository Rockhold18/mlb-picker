#!/usr/bin/env python3
"""Smart scheduler for MLB Picker — handles morning, lineup lock, and results runs.

Lineup lock runs only process games starting in the next ~3 hours whose
lineups are newly confirmed. Each game is only locked once.

Usage:
    python scheduler.py morning          # 8 AM: team-level preview picks
    python scheduler.py lineup_lock      # 11 AM/2 PM/5 PM/8 PM: lineup-aware updates
    python scheduler.py results          # 1 AM: score yesterday's picks
"""

import argparse
import logging
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from config import SEASON
from db import init_db, seed_priors, get_db
from data.mlb_api import get_schedule, get_pitcher_season_stats, get_all_team_records
from data.fip import compute_fip_from_stats
from data.fangraphs import refresh_fangraphs_stats
from data.lineups import fetch_and_cache_lineup_splits
from model.predict import predict_games, print_predictions, load_model
from model.features import build_feature_vector, FEATURE_NAMES
from output.dashboard import generate_dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def run_morning(date_str=None):
    """Morning run: refresh all data, generate team-level predictions."""
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")

    print(f"\n{'='*55}")
    print(f"  MORNING RUN — {date_str}")
    print(f"{'='*55}")

    init_db()
    seed_priors()

    # Import and run the full refresh from main
    from main import refresh_data
    refresh_data(date_str)

    # Run predictions
    picks = predict_games(date_str, run_type="morning")
    print_predictions(picks, date_str, "morning")

    # Generate dashboard
    generate_dashboard(date_str)

    high = sum(1 for p in picks if p["confidence"] == "HIGH")
    return f"{len(picks)} picks ready ({high} HIGH confidence)"


def run_lineup_lock(date_str=None):
    """Lineup lock run: fetch confirmed lineups and update predictions for upcoming games.

    Only processes games starting in the next ~3 hours that haven't been locked yet.
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    now = datetime.now()

    print(f"\n{'='*55}")
    print(f"  LINEUP LOCK — {date_str} ({now.strftime('%I:%M %p')})")
    print(f"{'='*55}")

    init_db()

    with get_db() as conn:
        # Get today's games
        games = conn.execute(
            "SELECT * FROM games WHERE game_date = ?", (date_str,)
        ).fetchall()

        if not games:
            print("  No games today.")
            return "No games today"

        # Determine which games to process:
        # Games starting in the next 3 hours that don't have a lineup_lock pick yet
        window_end = now + timedelta(hours=3)
        games_to_lock = []

        for g in games:
            # Check if already locked
            existing = conn.execute(
                "SELECT 1 FROM picks WHERE game_id = ? AND run_type = 'lineup_lock'",
                (g["game_id"],)
            ).fetchone()
            if existing:
                continue

            # Parse game time to determine if it's in our window
            # Game times are stored as "HH:MM PM ET" — parse them
            game_time_str = g["game_time"] or ""
            game_dt = _parse_game_time(game_time_str, date_str)

            if game_dt and game_dt <= window_end:
                games_to_lock.append(g)

        if not games_to_lock:
            print("  No unlocked games in the next 3 hours.")
            # Still regenerate dashboard in case results came in
            generate_dashboard(date_str)
            return "No games to lock"

        print(f"  {len(games_to_lock)} games to lock in this window\n")

        # Fetch lineups and batter splits for each game
        model, scaler = load_model()
        import pandas as pd
        locked = 0
        updated_picks = []

        for g in games_to_lock:
            lineup_data = fetch_and_cache_lineup_splits(g["game_id"], conn, SEASON)

            if not lineup_data:
                print(f"  {g['away_team']} @ {g['home_team']}: lineups not available, using morning pick")
                # Copy morning pick as lineup_lock
                morning = conn.execute(
                    "SELECT * FROM picks WHERE game_id = ? AND run_type = 'morning'",
                    (g["game_id"],)
                ).fetchone()
                if morning:
                    conn.execute("""
                        INSERT OR REPLACE INTO picks
                        (game_id, pick_date, run_type, predicted_winner, home_win_prob, confidence)
                        VALUES (?, ?, 'lineup_lock', ?, ?, ?)
                    """, (g["game_id"], date_str, morning["predicted_winner"],
                          morning["home_win_prob"], morning["confidence"]))
                    locked += 1
                continue

            # Build features with lineup-aware platoon data
            feats = build_feature_vector(g, conn)
            if feats is None:
                continue

            # Override platoon_wrc_diff with actual lineup OPS if available
            if lineup_data["home_lineup_ops"] and lineup_data["away_lineup_ops"]:
                # Scale OPS diff to roughly match wRC+ diff magnitude
                # OPS ~0.700-0.800, wRC+ ~80-120, so multiply OPS diff by ~150
                ops_diff = (lineup_data["home_lineup_ops"] - lineup_data["away_lineup_ops"]) * 150
                feats["platoon_wrc_diff"] = ops_diff
                logger.info(f"  {g['away_team']} @ {g['home_team']}: lineup OPS {lineup_data['away_lineup_ops']:.3f} vs {lineup_data['home_lineup_ops']:.3f}")

            # Predict
            feat_df = pd.DataFrame([feats])[FEATURE_NAMES].fillna(0)
            feat_scaled = scaler.transform(feat_df)
            home_win_prob = model.predict_proba(feat_scaled)[0][1]

            from config import HIGH_CONFIDENCE_THRESHOLD, MEDIUM_CONFIDENCE_THRESHOLD
            if home_win_prob >= 0.5:
                predicted_winner = g["home_team"]
                pick_prob = home_win_prob
            else:
                predicted_winner = g["away_team"]
                pick_prob = 1 - home_win_prob

            if pick_prob >= HIGH_CONFIDENCE_THRESHOLD:
                confidence = "HIGH"
            elif pick_prob >= MEDIUM_CONFIDENCE_THRESHOLD:
                confidence = "MEDIUM"
            else:
                confidence = "LEAN"

            # Check if pick changed from morning
            morning = conn.execute(
                "SELECT predicted_winner FROM picks WHERE game_id = ? AND run_type = 'morning'",
                (g["game_id"],)
            ).fetchone()
            changed = morning and morning["predicted_winner"] != predicted_winner
            change_marker = " ** CHANGED **" if changed else ""

            print(f"  {g['away_team']} @ {g['home_team']}: {predicted_winner} {pick_prob:.0%} {confidence}{change_marker}")

            conn.execute("""
                INSERT OR REPLACE INTO picks
                (game_id, pick_date, run_type, predicted_winner, home_win_prob, confidence)
                VALUES (?, ?, 'lineup_lock', ?, ?, ?)
            """, (g["game_id"], date_str, predicted_winner, round(home_win_prob, 4), confidence))
            locked += 1

    # Regenerate dashboard
    generate_dashboard(date_str)

    print(f"\n  Locked {locked} games")
    return f"{locked} games locked"


def run_results(date_str=None):
    """Results run: fetch final scores and mark picks correct/incorrect."""
    # Score yesterday's games by default
    date_str = date_str or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"\n{'='*55}")
    print(f"  RESULTS — {date_str}")
    print(f"{'='*55}")

    init_db()

    from data.mlb_api import get_game_results

    results = get_game_results(date_str)
    if not results:
        print(f"  No final results for {date_str}")
        return "No results"

    with get_db() as conn:
        scored = 0
        correct = 0
        total = 0

        for g in results:
            # Update game with final score
            conn.execute("""
                UPDATE games SET home_score = ?, away_score = ?, winner = ?, status = 'Final'
                WHERE game_id = ?
            """, (g["home_score"], g["away_score"], g["winner"], g["game_id"]))

            actual_winner = g["home_team"] if g["winner"] == "home" else g["away_team"]

            # Score all picks for this game (morning and lineup_lock)
            picks = conn.execute(
                "SELECT * FROM picks WHERE game_id = ?", (g["game_id"],)
            ).fetchall()

            for p in picks:
                is_correct = 1 if p["predicted_winner"] == actual_winner else 0
                conn.execute("""
                    UPDATE picks SET actual_winner = ?, correct = ?
                    WHERE game_id = ? AND run_type = ?
                """, (actual_winner, is_correct, g["game_id"], p["run_type"]))

                # Only count the best run type (lineup_lock > morning)
                if p["run_type"] == "lineup_lock" or (p["run_type"] == "morning" and not any(
                    pp["run_type"] == "lineup_lock" for pp in picks
                )):
                    total += 1
                    if is_correct:
                        correct += 1
                    scored += 1

    pct = f"{correct/total:.0%}" if total > 0 else "N/A"
    print(f"\n  Scored {scored} games: {correct}/{total} correct ({pct})")

    # Regenerate dashboard for the scored date
    generate_dashboard(date_str)

    return f"{correct}/{total} correct ({pct})"


def _parse_game_time(time_str, date_str):
    """Parse a game time like '07:05 PM ET' into a datetime."""
    try:
        time_str = time_str.replace(" ET", "").strip()
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %I:%M %p")
        return dt
    except (ValueError, AttributeError):
        return None


def main():
    parser = argparse.ArgumentParser(description="MLB Picker Scheduler")
    parser.add_argument("mode", choices=["morning", "lineup_lock", "results"],
                        help="Run mode")
    parser.add_argument("--date", default=None,
                        help="Date override (YYYY-MM-DD)")

    args = parser.parse_args()

    if args.mode == "morning":
        result = run_morning(args.date)
    elif args.mode == "lineup_lock":
        result = run_lineup_lock(args.date)
    elif args.mode == "results":
        result = run_results(args.date)

    print(f"\n  Done: {result}")


if __name__ == "__main__":
    main()
