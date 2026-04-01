"""Database schema migration — safely adds new columns to existing tables."""

import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "mlb_picker.db"

MIGRATIONS = [
    ("picks", "opener_flag", "TEXT"),
    ("pitcher_stats", "throw_hand", "TEXT"),
    ("team_stats", "wrc_plus_vs_lhp", "REAL"),
    ("team_stats", "wrc_plus_vs_rhp", "REAL"),
]


def get_existing_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def run_migrations(db_path):
    conn = sqlite3.connect(db_path)
    applied = 0

    for table, column, col_type in MIGRATIONS:
        existing = get_existing_columns(conn, table)
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"  Added {table}.{column} ({col_type})")
            applied += 1

    conn.commit()
    conn.close()

    if applied:
        print(f"  {applied} migration(s) applied")
    else:
        print("  Schema up to date")


if __name__ == "__main__":
    run_migrations(DB_PATH)
