"""FanGraphs API client for team-level wRC+ and bullpen ERA."""

import logging
from datetime import datetime, timedelta

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import SEASON

logger = logging.getLogger(__name__)

FANGRAPHS_API_BASE = "https://www.fangraphs.com/api/leaders/major-league/data"

FANGRAPHS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

# FanGraphs API uses different abbreviations for some teams
FANGRAPHS_ABBR_MAP = {
    "ATH": "OAK", "CHW": "CWS", "KCR": "KC",
    "SDP": "SD", "SFG": "SF", "TBR": "TB", "WSN": "WSH",
}


def _normalize_abbr(fg_abbr):
    """Convert FanGraphs abbreviation to our standard abbreviation."""
    return FANGRAPHS_ABBR_MAP.get(fg_abbr, fg_abbr)


def _fetch_team_stats(stats_type, season):
    """Fetch team-level stats from the FanGraphs JSON API.

    Args:
        stats_type: "bat" for batting, "rel" for relievers
        season: MLB season year

    Returns:
        List of team stat dicts, or empty list on failure.
    """
    params = {
        "pos": "all",
        "stats": stats_type,
        "lg": "all",
        "qual": 0,
        "type": 8,
        "season": season,
        "month": 0,
        "season1": season,
        "ind": 0,
        "team": "0,ts",
        "rost": 0,
        "age": 0,
        "filter": "",
        "players": 0,
        "pageitems": 2147483647,
        "pagenum": 1,
    }

    try:
        resp = requests.get(FANGRAPHS_API_BASE, params=params,
                            headers=FANGRAPHS_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])
    except requests.RequestException as e:
        logger.warning(f"FanGraphs API request failed ({stats_type}): {e}")
        return []
    except (ValueError, KeyError) as e:
        logger.warning(f"FanGraphs API response parse failed ({stats_type}): {e}")
        return []


def get_team_wrc_plus(season=None):
    """Get team wRC+ from FanGraphs API.

    Returns:
        Dict mapping team abbreviation → wRC+ value, or empty dict on failure.
    """
    season = season or SEASON
    teams = _fetch_team_stats("bat", season)

    results = {}
    for team in teams:
        abbr = _normalize_abbr(team.get("TeamNameAbb", ""))
        wrc_plus = team.get("wRC+")
        if abbr and wrc_plus is not None:
            try:
                results[abbr] = float(wrc_plus)
            except (ValueError, TypeError):
                continue
    return results


def get_team_wrc_plus_vs_hand(hand, season=None):
    """Get team wRC+ vs LHP or vs RHP from FanGraphs API.

    Args:
        hand: "L" or "R" — the pitcher's throwing hand
        season: MLB season year

    Returns:
        Dict mapping team abbreviation → wRC+ value vs that hand.
    """
    season = season or SEASON
    # FanGraphs: month=13 is vs LHP, month=14 is vs RHP
    month_code = 13 if hand == "L" else 14

    params = {
        "pos": "all", "stats": "bat", "lg": "all", "qual": 0, "type": 1,
        "season": season, "month": month_code, "season1": season,
        "ind": 0, "team": "0,ts", "rost": 0, "age": 0,
        "pageitems": 2147483647, "pagenum": 1,
    }

    try:
        resp = requests.get(FANGRAPHS_API_BASE, params=params,
                            headers=FANGRAPHS_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"FanGraphs platoon request failed (vs {hand}HP): {e}")
        return {}

    results = {}
    for team in data:
        abbr = _normalize_abbr(team.get("TeamNameAbb", ""))
        wrc = team.get("wRC+")
        if abbr and wrc is not None:
            try:
                results[abbr] = float(wrc)
            except (ValueError, TypeError):
                continue
    return results


def get_bullpen_era(season=None):
    """Get team bullpen (reliever) ERA from FanGraphs API.

    Returns:
        Dict mapping team abbreviation → bullpen ERA value, or empty dict on failure.
    """
    season = season or SEASON
    teams = _fetch_team_stats("rel", season)

    results = {}
    for team in teams:
        abbr = _normalize_abbr(team.get("TeamNameAbb", ""))
        era = team.get("ERA")
        if abbr and era is not None:
            try:
                results[abbr] = float(era)
            except (ValueError, TypeError):
                continue
    return results


def refresh_fangraphs_stats(db_conn, season=None, force=False):
    """Pull wRC+ and bullpen ERA from FanGraphs and update team_stats table.

    Skips refresh if data was updated within the last 7 days (unless force=True).
    """
    season = season or SEASON

    if not force:
        row = db_conn.execute(
            "SELECT MAX(updated_at) FROM team_stats WHERE season = ? AND wrc_plus IS NOT NULL", (season,)
        ).fetchone()
        if row and row[0]:
            try:
                last_update = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                if datetime.now() - last_update < timedelta(days=7):
                    logger.info("FanGraphs data is fresh (< 7 days), skipping refresh")
                    return
            except ValueError:
                pass  # Proceed with refresh if timestamp is unparseable

    wrc = get_team_wrc_plus(season)
    bullpen = get_bullpen_era(season)
    wrc_vs_lhp = get_team_wrc_plus_vs_hand("L", season)
    wrc_vs_rhp = get_team_wrc_plus_vs_hand("R", season)

    # Fall back to previous season if current season has sparse data
    if len(wrc) < 15 or len(bullpen) < 15:
        logger.info(f"Sparse {season} FanGraphs data ({len(wrc)} wRC+, {len(bullpen)} ERA) — falling back to {season - 1}")
        for getter, target in [
            (lambda: get_team_wrc_plus(season - 1), wrc),
            (lambda: get_bullpen_era(season - 1), bullpen),
            (lambda: get_team_wrc_plus_vs_hand("L", season - 1), wrc_vs_lhp),
            (lambda: get_team_wrc_plus_vs_hand("R", season - 1), wrc_vs_rhp),
        ]:
            prev = getter()
            for team, val in prev.items():
                target.setdefault(team, val)

    if not wrc and not bullpen:
        logger.warning("FanGraphs returned no data — skipping update")
        return

    from config import ABBR_TO_TEAM_ID

    all_teams = set(list(wrc.keys()) + list(bullpen.keys()) + list(wrc_vs_lhp.keys()) + list(wrc_vs_rhp.keys()))
    updated = 0
    for abbr in all_teams:
        team_id = ABBR_TO_TEAM_ID.get(abbr)
        if team_id is None:
            continue

        db_conn.execute("""
            INSERT INTO team_stats (team_id, team_name, season, wrc_plus, wrc_plus_vs_lhp, wrc_plus_vs_rhp, bullpen_era, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(team_id, season) DO UPDATE SET
                wrc_plus = COALESCE(excluded.wrc_plus, wrc_plus),
                wrc_plus_vs_lhp = COALESCE(excluded.wrc_plus_vs_lhp, wrc_plus_vs_lhp),
                wrc_plus_vs_rhp = COALESCE(excluded.wrc_plus_vs_rhp, wrc_plus_vs_rhp),
                bullpen_era = COALESCE(excluded.bullpen_era, bullpen_era),
                updated_at = datetime('now')
        """, (team_id, abbr, season, wrc.get(abbr), wrc_vs_lhp.get(abbr), wrc_vs_rhp.get(abbr), bullpen.get(abbr)))
        updated += 1

    logger.info(f"Updated FanGraphs stats for {updated} teams")
    print(f"  FanGraphs: updated {updated} teams (wRC+: {len(wrc)}, bullpen ERA: {len(bullpen)}, platoon splits: {len(wrc_vs_lhp)}L/{len(wrc_vs_rhp)}R)")
