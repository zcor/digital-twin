#!/usr/bin/env python3
"""Database schema setup for Gerrit Digital Twin.

Idempotent — safe to run multiple times.
Creates all tables, indexes, constraints. Enables WAL mode and foreign keys.
Sets 0600 permissions on the database file.
"""

import os
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "gerrit.db")


def init_db(db_path=None):
    db_path = db_path or DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")

    cur = conn.cursor()

    # --- sessions ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            mode TEXT NOT NULL,
            strategy JSON,
            topics_covered JSON,
            new_observations INT DEFAULT 0,
            updated_observations INT DEFAULT 0,
            session_summary TEXT
        )
    """)

    # --- messages ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            logged_via TEXT NOT NULL DEFAULT 'primary' CHECK(logged_via IN ('primary', 'reconciled'))
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session_id, timestamp)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_uuid ON messages(uuid)
    """)

    # --- observations ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dimension TEXT NOT NULL,
            facet TEXT NOT NULL,
            trait TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence REAL DEFAULT 0.3 CHECK(confidence >= 0.0 AND confidence <= 1.0),
            evidence_count INT DEFAULT 1 CHECK(evidence_count >= 1),
            specificity TEXT NOT NULL CHECK(specificity IN ('vague', 'general', 'specific', 'precise')),
            first_observed TEXT NOT NULL,
            last_confirmed TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'claude',
            last_validated_at TEXT,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            superseded_by INT REFERENCES observations(id)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_observations_dim_facet ON observations(dimension, facet)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_observations_confidence ON observations(confidence)
    """)

    # --- observation_messages (join table) ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS observation_messages (
            observation_id INT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
            message_id INT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
            relation_type TEXT NOT NULL CHECK(relation_type IN ('source', 'confirmation', 'contradiction')),
            PRIMARY KEY (observation_id, message_id, relation_type)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_obs_msgs_message ON observation_messages(message_id)
    """)

    # --- observation_links ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS observation_links (
            from_observation_id INT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
            to_observation_id INT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
            link_type TEXT NOT NULL CHECK(link_type IN ('contradicts', 'supersedes', 'contextualizes')),
            context TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            PRIMARY KEY (from_observation_id, to_observation_id, link_type)
        )
    """)

    # --- exemplars ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS exemplars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_id INT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
            source_message_id INT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
            quote TEXT NOT NULL,
            context TEXT,
            significance TEXT
        )
    """)

    # --- vocabulary ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vocabulary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term TEXT NOT NULL,
            normalized_term TEXT NOT NULL,
            category TEXT NOT NULL CHECK(category IN ('catchphrase', 'filler', 'jargon', 'slang', 'avoids', 'other')),
            frequency TEXT CHECK(frequency IN ('constant', 'frequent', 'occasional', 'rare')),
            context TEXT,
            example_usage JSON,
            first_observed TEXT NOT NULL,
            session_count INT DEFAULT 1,
            UNIQUE(normalized_term)
        )
    """)

    # --- opinions ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS opinions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            domain TEXT,
            stance TEXT NOT NULL,
            strength REAL DEFAULT 0.5 CHECK(strength >= 0.0 AND strength <= 1.0),
            nuance TEXT,
            confidence REAL DEFAULT 0.5 CHECK(confidence >= 0.0 AND confidence <= 1.0),
            last_confirmed TEXT,
            may_have_changed BOOLEAN DEFAULT 0
        )
    """)

    # --- opinion_messages (join table) ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS opinion_messages (
            opinion_id INT NOT NULL REFERENCES opinions(id) ON DELETE CASCADE,
            message_id INT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
            PRIMARY KEY (opinion_id, message_id)
        )
    """)

    # --- gaps ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dimension TEXT NOT NULL,
            facet TEXT,
            description TEXT NOT NULL,
            priority REAL DEFAULT 0.5 CHECK(priority >= 0.0 AND priority <= 1.0),
            suggested_approaches JSON,
            status TEXT DEFAULT 'open' CHECK(status IN ('open', 'partially_addressed', 'resolved')),
            resolved_by_session TEXT REFERENCES sessions(session_id)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_gaps_priority ON gaps(priority DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_gaps_status ON gaps(status)
    """)

    # --- pending_observations ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
            candidate_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'accepted', 'rejected')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)

    # --- meta ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Seed meta (only if not already set)
    meta_defaults = {
        "schema_version": "1",
        "current_mode": "interview",
        "total_sessions": "0",
        "disclosure_mode": "on",
        "impersonation_enabled": "false",
    }
    for k, v in meta_defaults.items():
        cur.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()

    # Set file permissions to 0600
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass  # May fail on some filesystems; non-fatal

    return db_path


if __name__ == "__main__":
    path = init_db()
    print(f"Database initialized: {path}")
