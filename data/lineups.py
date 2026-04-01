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


def fetch_and_cache_lineup_splits(game_id, conn, season=None):
    """Fetch lineup for a game and cache batter splits in the DB.

    Returns:
        Dict with home_lineup_ops and away_lineup_ops (avg OPS vs opposing starter hand),
        or None if lineups aren't posted yet.
    """
    season = season or SEASON

    lineup = get_lineup(game_id)
    if not lineup:
        logger.info(f"  Game {game_id}: lineups not posted yet")
        return None

    # Get opposing starter hands from DB
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if not game:
        return None

    home_starter_hand = _get_hand(game["home_starter_id"], conn)
    away_starter_hand = _get_hand(game["away_starter_id"], conn)

    # Compute lineup OPS for each side
    home_ops = _compute_lineup_ops(
        lineup["home_lineup"], away_starter_hand, conn, season
    )
    away_ops = _compute_lineup_ops(
        lineup["away_lineup"], home_starter_hand, conn, season
    )

    return {
        "home_lineup_ops": home_ops,
        "away_lineup_ops": away_ops,
        "home_lineup_size": len(lineup["home_lineup"]),
        "away_lineup_size": len(lineup["away_lineup"]),
    }


def _compute_lineup_ops(batter_ids, pitcher_hand, conn, season):
    """Compute average OPS of a lineup against a specific pitcher hand.

    For switch hitters, uses the opposite-hand split (which is the side they'd bat from).
    Falls back to overall OPS if splits are unavailable.

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

        # Use the batter's split against the opposing pitcher's hand.
        # Switch hitters (S) always bat from the opposite side, so they
        # get their vs-LHP stats when facing a LHP (batting right-handed)
        # and vs-RHP stats when facing a RHP (batting left-handed).
        # This is the same lookup regardless of bat side.
        if pitcher_hand == "L":
            ops = splits.get("ops_vs_lhp")
        else:
            ops = splits.get("ops_vs_rhp")

        if ops is not None:
            ops_values.append(ops)

    if len(ops_values) < 5:
        return None  # Too few batters with split data

    return sum(ops_values) / len(ops_values)


def _get_or_fetch_splits(player_id, conn, season):
    """Get batter splits from cache or fetch from API.

    Returns:
        Dict with bat_side, ops_vs_lhp, ops_vs_rhp, or None.
    """
    # Check cache first
    row = conn.execute(
        "SELECT * FROM batter_splits WHERE player_id = ? AND season = ?",
        (player_id, season)
    ).fetchone()

    if row:
        return dict(row)

    # Also check previous season
    if not row:
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

    # Cache in DB
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
