# MLB Game Picker

Daily MLB win probability model for a head-to-head pick accuracy contest.

## Stack
- Python 3.9+, SQLite, scikit-learn (logistic regression), pandas
- Data: MLB Stats API (free), FanGraphs JSON API (free)
- Dashboard: Self-contained HTML deployed to GitHub Pages
- Automation: GitHub Actions (6 daily cron runs)
- Repo: github.com/ArtificialDegeneracy/mlb-picker

## Architecture

```
main.py              CLI entry point (refresh, predict, init, dashboard, status)
scheduler.py         Automation brain (morning, lineup_lock, results modes)
config.py            Season config, thresholds, team mappings, park factors
db.py                SQLite schema + connection management

data/
  mlb_api.py         MLB Stats API client (schedule, pitcher stats, team records, lineups, batter splits)
  fip.py             FIP computation from pitching components
  fangraphs.py       FanGraphs JSON API for wRC+, bullpen ERA, platoon splits
  historical.py      Pull 2022-2025 games for model training
  lineups.py         Lineup-aware features for lineup lock runs

model/
  features.py        Feature engineering (FEATURE_NAMES defines the model input)
  predict.py         Logistic regression training, prediction, opener detection

output/
  dashboard.py       HTML dashboard generator (Today's Picks, Season Tracker, Pick History)

.github/workflows/
  daily-picks.yml    GitHub Actions: 6 cron runs + manual dispatch with date override
```

## Model
- Logistic regression trained on 2022-2024 MLB data (~7,287 games)
- Validated on 2025: 67.9% HIGH tier accuracy at 63% confidence threshold
- Features: FIP differential, team quality (prior-blended), platoon wRC+, park factors, bullpen ERA, home flag
- Opener detection: dampens confidence 40% toward 50% when listed starter has <10 career GS

## Daily Run Schedule (GitHub Actions)
All times ET (crons are UTC, DST-adjusted for summer only):
- 8 AM: Morning picks (team-level)
- 11 AM / 2 PM / 5 PM / 8 PM: Lineup lock (batter-level splits, 3-hour window)
- 1 AM: Results (score previous day)

Dashboard: https://artificialdegeneracy.github.io/mlb-picker/

## Key Patterns
- **Per-game dedup**: Dashboard queries must prefer lineup_lock over morning picks using:
  ```sql
  AND p.run_type = (SELECT p2.run_type FROM picks p2 WHERE p2.game_id = p.game_id
    ORDER BY CASE p2.run_type WHEN 'lineup_lock' THEN 0 ELSE 1 END LIMIT 1)
  ```
- **Pitcher stats fallback**: Current season → previous season. Stores both rows keyed by actual season.
- **Early-season guard**: Team W-L records need 10+ games before blending with priors.
- **Artifacts**: DB + model pkl files persist between GitHub Actions runs via upload/download artifacts. Seed files in `seed/` as fallback.

## Known Issues (as of 2026-04-01)
- SEASON is hardcoded to 2026 in config.py — needs to be dynamic
- Game time UTC→ET uses month approximation for DST (wrong March 1-13, late Oct)
- GitHub Actions crons are DST-only — off by 1 hour Nov-Mar
- Model is never retrained with in-season data
- FIP constant hardcoded at 3.10, never recomputed
- No doubleheader awareness (Game 2 treated same as Game 1)
- Picks dedup logic varies across dashboard queries (one place uses MAX which is alphabetically wrong)
- Missing DB indices on game_id, player_id, team_name

## Important Context
- Backfill runs for completed games have lookahead bias (team W-L includes game outcomes before predicting). Only run morning for future/same-day games.
- If cloud artifacts get corrupted, delete via `gh api -X DELETE` and re-run to fall back to seed.
- The user observed the model mostly picks favorites but underdog picks hit well — contrarian picks may be the real edge.
- Series context (sweep attempts, G1 momentum) is shown on cards but NOT used in the model (tested, didn't improve accuracy).
