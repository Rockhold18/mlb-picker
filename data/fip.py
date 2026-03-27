"""FIP (Fielding Independent Pitching) computation from raw pitching stats."""

import logging

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import DEFAULT_FIP_CONSTANT

logger = logging.getLogger(__name__)


def compute_fip(hr, bb, hbp, k, ip, fip_constant=None):
    """Compute FIP from pitching components.

    Formula: ((13*HR + 3*(BB+HBP) - 2*K) / IP) + FIP_constant

    Args:
        hr: Home runs allowed
        bb: Walks
        hbp: Hit by pitch
        k: Strikeouts
        ip: Innings pitched
        fip_constant: League-specific constant (~3.10). Uses default if None.

    Returns:
        FIP value (float), or None if insufficient innings.
    """
    if fip_constant is None:
        fip_constant = DEFAULT_FIP_CONSTANT

    if ip is None or ip < 1.0:
        return None

    try:
        fip = ((13 * hr + 3 * (bb + hbp) - 2 * k) / ip) + fip_constant
        return round(fip, 2)
    except (TypeError, ZeroDivisionError):
        return None


def compute_fip_from_stats(pitcher_stats, fip_constant=None):
    """Compute FIP from a pitcher stats dict (as returned by mlb_api).

    Args:
        pitcher_stats: Dict with keys hr, bb, hbp, k, ip
        fip_constant: Override constant

    Returns:
        FIP value or None
    """
    if pitcher_stats is None:
        return None
    return compute_fip(
        hr=pitcher_stats.get("hr", 0),
        bb=pitcher_stats.get("bb", 0),
        hbp=pitcher_stats.get("hbp", 0),
        k=pitcher_stats.get("k", 0),
        ip=pitcher_stats.get("ip", 0),
        fip_constant=fip_constant,
    )


def compute_league_fip_constant(league_era, league_hr, league_bb, league_hbp, league_k, league_ip):
    """Derive the FIP constant from league-wide totals.

    FIP_constant = lgERA - ((13*lgHR + 3*(lgBB+lgHBP) - 2*lgK) / lgIP)

    This is typically ~3.10 but varies year to year.
    """
    if league_ip is None or league_ip < 100:
        return DEFAULT_FIP_CONSTANT
    try:
        raw = (13 * league_hr + 3 * (league_bb + league_hbp) - 2 * league_k) / league_ip
        constant = league_era - raw
        return round(constant, 2)
    except (TypeError, ZeroDivisionError):
        return DEFAULT_FIP_CONSTANT
