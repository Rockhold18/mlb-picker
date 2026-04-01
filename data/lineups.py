"""Lineup-aware features for lineup lock predictions."""

import logging
import time

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import SEASON, REQUEST_DELAY
from db import get_db
from data.mlb_api import get_lineup, get_batter_info, get_batter_splits

logger = logging.getLogger(__name__)

# Lineup dampening: if today's lineup OPS is this much below the rolling average, dampen confidence
LINEUP_WEAK_THRESHOLD = 0.04  # 4% below normal triggers dampening
LINEUP_DAMPEN_FACTOR = 0.25   # Shrink distance from 50% by 25%


def fetch_and_cache_lineup(game_id, conn, season=None):
    """Fetch lineup for a game, cache in game_lineups, compute OPS and strength.

    Returns:
        Dict with lineup data for both teams, or None if lineups aren't posted.
    """
    season = season or SEASON

    lineup = get_lineup(game_id)
    if not lineup:
        logger.info(f"  Game {game_id}: lineups not posted yet")
        return None

    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if not game:
        return None

    home_starter_hand = _get_hand(game["home_starter_id"], conn)
    away_starter_hand = _get_hand(game["away_starter_id"], conn)

    result = {}
    for side, batter_ids, opp_hand in [
        ("home", lineup["home_lineup"], away_starter_hand),
        ("away", lineup["away_lineup"], home_starter_hand),
    ]:
        team = game[f"{side}_team"]

        # Fetch and store each batter's splits
        batter_ops_list = []
        for pos, pid in enumerate(batter_ids, 1):
            splits = _get_or_fetch_splits(pid, conn, season)
            if not splits:
                continue

            # OPS against this pitcher's hand
            if opp_hand == "L":
                ops = splits.get("ops_vs_lhp")
            else:
                ops = splits.get("ops_vs_rhp")

            # Store in game_lineups table
            conn.execute("""
                INSERT OR REPLACE INTO game_lineups
                (game_id, team, player_id, lineup_position, player_name,
                 bat_side, ops_vs_lhp, ops_vs_rhp, lineup_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                game_id, team, pid, pos,
                splits.get("player_name", "Unknown"),
                splits.get("bat_side"),
                splits.get("ops_vs_lhp"),
                splits.get("ops_vs_rhp"),
                game["game_date"],
            ))

            if ops is not None:
                # Weight by lineup position (top of order matters more)
                weight = 1.0 + max(0, (5 - pos) * 0.1)  # 1-4 get 1.1-1.4x, 5-9 get 1.0x
                batter_ops_list.append((ops, weight))

        # Compute weighted lineup OPS
        if len(batter_ops_list) >= 5:
            total_weight = sum(w for _, w in batter_ops_list)
            weighted_ops = sum(ops * w for ops, w in batter_ops_list) / total_weight
        else:
            weighted_ops = None

        # Get rolling baseline (team's avg lineup OPS over last 10 games)
        baseline_ops = _get_lineup_baseline(team, game["game_date"], conn)

        # Detect key absences
        missing_regulars = _detect_missing_regulars(
            team, game["game_date"], [pid for pid in batter_ids], conn
        )

        # Compute strength gap
        lineup_gap = None
        lineup_pct_change = None
        if weighted_ops and baseline_ops and baseline_ops > 0:
            lineup_gap = weighted_ops - baseline_ops
            lineup_pct_change = lineup_gap / baseline_ops

        result[side] = {
            "lineup_ops": weighted_ops,
            "baseline_ops": baseline_ops,
            "lineup_gap": lineup_gap,
            "lineup_pct_change": lineup_pct_change,
            "is_weakened": lineup_pct_change is not None and lineup_pct_change < -LINEUP_WEAK_THRESHOLD,
            "missing_regulars": missing_regulars,
            "batter_count": len(batter_ops_list),
        }

    return result


def _get_lineup_baseline(team, game_date, conn):
    """Get team's average lineup OPS over their last 10 games.

    Computes from stored game_lineups data.
    """
    rows = conn.execute("""
        SELECT AVG(avg_ops) as baseline FROM (
            SELECT gl.game_id, AVG(
                CASE WHEN gl.ops_vs_rhp IS NOT NULL THEN gl.ops_vs_rhp ELSE gl.ops_vs_lhp END
            ) as avg_ops
            FROM game_lineups gl
            WHERE gl.team = ? AND gl.lineup_date < ?
            GROUP BY gl.game_id
            ORDER BY gl.lineup_date DESC
            LIMIT 10
        )
    """, (team, game_date)).fetchone()

    return rows["baseline"] if rows and rows["baseline"] else None


def _detect_missing_regulars(team, game_date, today_pids, conn):
    """Detect regular players who are NOT in today's lineup.

    A "regular" is a player who appeared in 70%+ of the team's last 10 lineups.
    Returns list of dicts with player info for missing regulars.
    """
    # Get players who appeared in recent lineups
    recent = conn.execute("""
        SELECT player_id, player_name,
               COUNT(DISTINCT game_id) as appearances,
               AVG(COALESCE(ops_vs_rhp, ops_vs_lhp, 0)) as avg_ops
        FROM game_lineups
        WHERE team = ? AND lineup_date < ?
          AND game_id IN (
            SELECT DISTINCT game_id FROM game_lineups
            WHERE team = ? AND lineup_date < ?
            ORDER BY lineup_date DESC
            LIMIT 10
          )
        GROUP BY player_id
    """, (team, game_date, team, game_date)).fetchall()

    if not recent:
        return []

    # Count total recent games
    total_games = conn.execute("""
        SELECT COUNT(DISTINCT game_id) FROM game_lineups
        WHERE team = ? AND lineup_date < ?
        ORDER BY lineup_date DESC
        LIMIT 10
    """, (team, game_date)).fetchone()[0]

    if total_games < 3:
        return []  # Not enough data to determine regulars

    missing = []
    for r in recent:
        appearance_rate = r["appearances"] / total_games
        if appearance_rate >= 0.7 and r["player_id"] not in today_pids:
            missing.append({
                "player_id": r["player_id"],
                "player_name": r["player_name"],
                "avg_ops": r["avg_ops"],
                "appearance_rate": appearance_rate,
            })

    # Sort by OPS so highest-impact absence is first
    missing.sort(key=lambda x: x["avg_ops"] or 0, reverse=True)
    return missing


def _compute_lineup_ops(batter_ids, pitcher_hand, conn, season):
    """Compute average OPS of a lineup against a specific pitcher hand.

    Returns:
        Average OPS (float), or None if insufficient data.
    """
    if not batter_ids or not pitcher_hand:
        return None

    ops_values = []
    for pid in batter_ids:
        splits = _get_or_fetch_splits(pid, conn, season)
        if not splits:
            continue

        if pitcher_hand == "L":
            ops = splits.get("ops_vs_lhp")
        else:
            ops = splits.get("ops_vs_rhp")

        if ops is not None:
            ops_values.append(ops)

    if len(ops_values) < 5:
        return None

    return sum(ops_values) / len(ops_values)


def _get_or_fetch_splits(player_id, conn, season):
    """Get batter splits from cache or fetch from API."""
    # Check current season cache
    row = conn.execute(
        "SELECT * FROM batter_splits WHERE player_id = ? AND season = ?",
        (player_id, season)
    ).fetchone()
    if row:
        return dict(row)

    # Check previous season
    row = conn.execute(
        "SELECT * FROM batter_splits WHERE player_id = ? AND season = ?",
        (player_id, season - 1)
    ).fetchone()
    if row:
        return dict(row)

    # Fetch from API
    info = get_batter_info(player_id)
    splits = get_batter_splits(player_id, season)

    if not info or not splits:
        return None

    conn.execute("""
        INSERT OR REPLACE INTO batter_splits
        (player_id, player_name, bat_side, season, ops_vs_lhp, ops_vs_rhp, ab_vs_lhp, ab_vs_rhp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        player_id, info["player_name"], info["bat_side"], season,
        splits["ops_vs_lhp"], splits["ops_vs_rhp"],
        splits["ab_vs_lhp"], splits["ab_vs_rhp"],
    ))

    return {
        "player_id": player_id,
        "player_name": info["player_name"],
        "bat_side": info["bat_side"],
        "ops_vs_lhp": splits["ops_vs_lhp"],
        "ops_vs_rhp": splits["ops_vs_rhp"],
    }


def _get_hand(pitcher_id, conn):
    """Get pitcher throw hand from DB."""
    if not pitcher_id:
        return None
    row = conn.execute(
        "SELECT throw_hand FROM pitcher_stats WHERE player_id = ? AND throw_hand IS NOT NULL ORDER BY season DESC LIMIT 1",
        (pitcher_id,)
    ).fetchone()
    return row["throw_hand"] if row else None
