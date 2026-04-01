"""Database schema migration — safely adds new columns and tables."""

import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "mlb_picker.db"

# Column migrations: (table, column_name, column_type)
COLUMN_MIGRATIONS = [
    ("picks", "opener_flag", "TEXT"),
    ("pitcher_stats", "throw_hand", "TEXT"),
    ("team_stats", "wrc_plus_vs_lhp", "REAL"),
    ("team_stats", "wrc_plus_vs_rhp", "REAL"),
    ("games", "roof_type", "TEXT"),
    ("games", "weather_temp", "INTEGER"),
    ("games", "weather_wind", "TEXT"),
    ("games", "weather_condition", "TEXT"),
]

# Table migrations: (table_name, create_statement)
TABLE_MIGRATIONS = [
    ("game_lineups", """
        CREATE TABLE IF NOT EXISTS game_lineups (
            game_id TEXT,
            team TEXT,
            player_id INTEGER,
            lineup_position INTEGER,
            player_name TEXT,
            bat_side TEXT,
            ops_vs_lhp REAL,
            ops_vs_rhp REAL,
            lineup_date TEXT,
            PRIMARY KEY (game_id, team, lineup_position)
        )
    """),
]

INDEX_MIGRATIONS = [
    "CREATE INDEX IF NOT EXISTS idx_lineups_team_date ON game_lineups(team, lineup_date)",
]


def get_existing_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def get_existing_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def run_migrations(db_path):
    conn = sqlite3.connect(db_path)
    applied = 0

    # Add new tables
    existing_tables = get_existing_tables(conn)
    for table_name, create_sql in TABLE_MIGRATIONS:
        if table_name not in existing_tables:
            conn.execute(create_sql)
            print(f"  Created table {table_name}")
            applied += 1

    # Add new columns
    for table, column, col_type in COLUMN_MIGRATIONS:
        existing = get_existing_columns(conn, table)
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"  Added {table}.{column} ({col_type})")
            applied += 1

    # Add indices
    for idx_sql in INDEX_MIGRATIONS:
        try:
            conn.execute(idx_sql)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()

    if applied:
        print(f"  {applied} migration(s) applied")
    else:
        print("  Schema up to date")


if __name__ == "__main__":
    run_migrations(DB_PATH)
