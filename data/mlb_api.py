"""MLB Stats API client for schedule, pitcher stats, team records, and game results."""

import time
import logging
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import MLB_API_BASE, REQUEST_TIMEOUT, REQUEST_DELAY, TEAM_ID_TO_ABBR, SEASON

logger = logging.getLogger(__name__)


def _get_et_offset(dt_utc):
    """Calculate UTC offset for US Eastern Time (handles DST properly).

    DST runs from second Sunday of March at 2 AM to first Sunday of November at 2 AM.
    During DST: ET = UTC - 4. During EST: ET = UTC - 5.
    """
    year = dt_utc.year
    # Second Sunday of March
    mar1 = datetime(year, 3, 1)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)  # second Sunday
    dst_start = dst_start.replace(hour=7)  # 2 AM ET = 7 AM UTC

    # First Sunday of November
    nov1 = datetime(year, 11, 1)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)  # first Sunday
    dst_end = dst_end.replace(hour=6)  # 2 AM ET = 6 AM UTC (still DST at that point)

    if dst_start <= dt_utc < dst_end:
        return 4  # EDT
    return 5  # EST


# Persistent session with retry logic
_session = None


def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        _session.mount("https://", HTTPAdapter(max_retries=retry))
    return _session


def _api_get(endpoint, params=None):
    """Make a GET request to the MLB Stats API."""
    url = f"{MLB_API_BASE}{endpoint}"
    try:
        resp = _get_session().get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.warning(f"API request failed: {url} — {e}")
        return None


def get_schedule(date_str):
    """Get today's games with probable starters.

    Args:
        date_str: Date in YYYY-MM-DD format

    Returns:
        List of game dicts with keys: game_id, game_date, home_team, away_team,
        home_team_id, away_team_id, home_starter_id, away_starter_id,
        home_starter_name, away_starter_name, game_time, venue, status
    """
    data = _api_get("/schedule", params={
        "sportId": 1,
        "date": date_str,
        "hydrate": "probablePitcher,linescore,team",
    })
    if not data or not data.get("dates"):
        logger.info(f"No games found for {date_str}")
        return []

    games = []
    for date_entry in data["dates"]:
        for game in date_entry.get("games", []):
            home = game.get("teams", {}).get("home", {})
            away = game.get("teams", {}).get("away", {})

            home_team_id = home.get("team", {}).get("id")
            away_team_id = away.get("team", {}).get("id")

            home_starter = home.get("probablePitcher", {})
            away_starter = away.get("probablePitcher", {})

            # Parse game time (UTC → ET, which is UTC-4 during DST, UTC-5 otherwise)
            game_date_raw = game.get("gameDate", "")
            game_time = ""
            if game_date_raw:
                try:
                    dt_utc = datetime.strptime(game_date_raw, "%Y-%m-%dT%H:%M:%SZ")
                    dt = dt_utc - timedelta(hours=_get_et_offset(dt_utc))
                    game_time = dt.strftime("%I:%M %p") + " ET"
                except ValueError:
                    game_time = game_date_raw

            # Determine status
            status = game.get("status", {}).get("abstractGameState", "Preview")

            # Extract scores if final
            linescore = game.get("linescore", {})
            home_score = None
            away_score = None
            winner = None
            if status == "Final":
                home_score = linescore.get("teams", {}).get("home", {}).get("runs")
                away_score = linescore.get("teams", {}).get("away", {}).get("runs")
                if home_score is not None and away_score is not None:
                    winner = "home" if home_score > away_score else "away"

            # Venue info
            venue_info = game.get("venue", {})
            venue_name = venue_info.get("name", "")
            roof_type = venue_info.get("fieldInfo", {}).get("roofType", "")

            games.append({
                "game_id": str(game.get("gamePk", "")),
                "game_date": date_str,
                "home_team": TEAM_ID_TO_ABBR.get(home_team_id, str(home_team_id)),
                "away_team": TEAM_ID_TO_ABBR.get(away_team_id, str(away_team_id)),
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
                "home_starter_id": home_starter.get("id"),
                "away_starter_id": away_starter.get("id"),
                "home_starter_name": home_starter.get("fullName", "TBD"),
                "away_starter_name": away_starter.get("fullName", "TBD"),
                "game_time": game_time,
                "venue": venue_name,
                "roof_type": roof_type,
                "status": status,
                "home_score": home_score,
                "away_score": away_score,
                "winner": winner,
            })

    return games


def get_pitcher_season_stats(player_id, season=None):
    """Get a pitcher's season stats. Falls back to previous season if current is empty.

    Returns:
        Dict with era, ip, k, bb, hbp, hr, hits, games_started, k_per_9, bb_per_9
        or None if unavailable.
    """
    if player_id is None:
        return None

    season = season or SEASON
    time.sleep(REQUEST_DELAY)

    # Try current season, then fall back to previous season
    for try_season in [season, season - 1]:
        data = _api_get(f"/people/{player_id}/stats", params={
            "stats": "season",
            "season": try_season,
            "group": "pitching",
        })
        if not data:
            continue
        stats_list = data.get("stats", [])
        if not stats_list:
            continue
        splits = stats_list[0].get("splits", [])
        if splits:
            if try_season != season:
                logger.info(f"  Using {try_season} stats for pitcher {player_id} (no {season} data)")
            break
        time.sleep(REQUEST_DELAY)
    else:
        return None

    s = splits[0].get("stat", {})

    ip_str = s.get("inningsPitched", "0")
    try:
        ip = float(ip_str)
    except (ValueError, TypeError):
        ip = 0.0

    k = s.get("strikeOuts", 0)
    bb = s.get("baseOnBalls", 0)

    return {
        "era": s.get("era", None),
        "ip": ip,
        "k": k,
        "bb": bb,
        "hbp": s.get("hitByPitch", 0),
        "hr": s.get("homeRuns", 0),
        "hits": s.get("hits", 0),
        "games_started": s.get("gamesStarted", 0),
        "k_per_9": round(k * 9 / ip, 2) if ip > 0 else None,
        "bb_per_9": round(bb * 9 / ip, 2) if ip > 0 else None,
        "player_name": None,  # filled by caller from schedule data
        "actual_season": try_season,  # which season this data came from
    }


def get_pitcher_hand(player_id):
    """Get a pitcher's throwing hand.

    Returns:
        "L" or "R", or None if unavailable.
    """
    if player_id is None:
        return None

    time.sleep(REQUEST_DELAY)
    data = _api_get(f"/people/{player_id}")
    if not data:
        return None

    people = data.get("people", [])
    if not people:
        return None

    hand = people[0].get("pitchHand", {}).get("code")
    return hand  # "L" or "R"


def get_pitcher_hands_bulk(player_ids):
    """Get throwing hand for multiple pitchers efficiently.

    Args:
        player_ids: List of player IDs

    Returns:
        Dict mapping player_id → "L" or "R"
    """
    results = {}
    for pid in player_ids:
        if pid is None:
            continue
        hand = get_pitcher_hand(pid)
        if hand:
            results[pid] = hand
    return results


def get_team_record(team_id, season=None):
    """Get a team's W-L record.

    Returns:
        Dict with wins, losses, or None if unavailable.
    """
    season = season or SEASON
    data = _api_get("/standings", params={
        "leagueId": "103,104",
        "season": season,
    })
    if not data:
        return None

    for record_group in data.get("records", []):
        for team_record in record_group.get("teamRecords", []):
            if team_record.get("team", {}).get("id") == team_id:
                return {
                    "wins": team_record.get("wins", 0),
                    "losses": team_record.get("losses", 0),
                }
    return None


def get_all_team_records(season=None):
    """Get W-L records for all teams.

    Returns:
        Dict mapping team_id → {wins, losses}
    """
    season = season or SEASON
    data = _api_get("/standings", params={
        "leagueId": "103,104",
        "season": season,
    })
    if not data:
        return {}

    records = {}
    for record_group in data.get("records", []):
        for team_record in record_group.get("teamRecords", []):
            tid = team_record.get("team", {}).get("id")
            if tid:
                records[tid] = {
                    "wins": team_record.get("wins", 0),
                    "losses": team_record.get("losses", 0),
                }
    return records


def get_game_weather(game_id):
    """Get weather data for a game from the live feed.

    Returns:
        Dict with temp, wind, condition, roof_type — or None if unavailable.
    """
    try:
        resp = _get_session().get(
            f"https://statsapi.mlb.com/api/v1.1/game/{game_id}/feed/live",
            timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    game_data = data.get("gameData", {})
    weather = game_data.get("weather", {})
    venue = game_data.get("venue", {})

    if not weather:
        return None

    temp_str = weather.get("temp", "")
    try:
        temp = int(temp_str)
    except (ValueError, TypeError):
        temp = None

    return {
        "temp": temp,
        "wind": weather.get("wind", ""),
        "condition": weather.get("condition", ""),
        "roof_type": venue.get("fieldInfo", {}).get("roofType", ""),
    }


def get_game_results(date_str):
    """Get final scores for all completed games on a date.

    Returns:
        List of dicts with game_id, home_score, away_score, winner, status
    """
    games = get_schedule(date_str)
    return [g for g in games if g["status"] == "Final"]


def get_lineup(game_id):
    """Get confirmed batting order for both teams from the live game feed.

    Returns:
        Dict with home_lineup and away_lineup, each a list of player_id ints.
        Returns None if lineups aren't posted yet.
    """
    try:
        resp = _get_session().get(
            f"https://statsapi.mlb.com/api/v1.1/game/{game_id}/feed/live",
            timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    boxscore = data.get("liveData", {}).get("boxscore", {})
    home_order = boxscore.get("teams", {}).get("home", {}).get("battingOrder", [])
    away_order = boxscore.get("teams", {}).get("away", {}).get("battingOrder", [])

    if not home_order or not away_order:
        return None

    return {
        "home_lineup": home_order,
        "away_lineup": away_order,
    }


def get_batter_info(player_id):
    """Get a batter's name and bat side.

    Returns:
        Dict with player_name, bat_side ("L", "R", or "S"), or None.
    """
    if player_id is None:
        return None

    time.sleep(REQUEST_DELAY)
    data = _api_get(f"/people/{player_id}")
    if not data or not data.get("people"):
        return None

    p = data["people"][0]
    return {
        "player_name": p.get("fullName", "Unknown"),
        "bat_side": p.get("batSide", {}).get("code"),
    }


def get_batter_splits(player_id, season=None):
    """Get a batter's OPS vs LHP and vs RHP.

    Returns:
        Dict with ops_vs_lhp, ops_vs_rhp, ab_vs_lhp, ab_vs_rhp, or None.
    """
    if player_id is None:
        return None

    season = season or SEASON
    time.sleep(REQUEST_DELAY)

    data = _api_get(f"/people/{player_id}/stats", params={
        "stats": "statSplits",
        "group": "hitting",
        "season": season,
        "sitCodes": "vl,vr",
    })
    if not data:
        return None

    result = {"ops_vs_lhp": None, "ops_vs_rhp": None, "ab_vs_lhp": 0, "ab_vs_rhp": 0}
    for split_group in data.get("stats", []):
        for split in split_group.get("splits", []):
            desc = split.get("split", {}).get("description", "")
            s = split.get("stat", {})
            ops = s.get("ops")
            ab = s.get("atBats", 0)
            try:
                ops = float(ops) if ops else None
            except (ValueError, TypeError):
                ops = None

            if "Left" in desc:
                result["ops_vs_lhp"] = ops
                result["ab_vs_lhp"] = ab
            elif "Right" in desc:
                result["ops_vs_rhp"] = ops
                result["ab_vs_rhp"] = ab

    # Fall back to previous season if current season has no data (one level only)
    if result["ops_vs_lhp"] is None and result["ops_vs_rhp"] is None and season > 2024:
        return get_batter_splits(player_id, season - 1)

    return result
