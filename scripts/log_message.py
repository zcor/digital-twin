#!/usr/bin/env python3
"""Per-message logging for Gerrit Digital Twin.

Reads content from stdin, generates UUID, stores in DB and fallback transcript.
Supports observation candidate validation and impersonation guards.

Usage:
    echo "message" | python3 scripts/log_message.py --role user --session SESSION_ID --content-stdin
    echo '{"dim":...}' | python3 scripts/log_message.py --observation-candidate --session SESSION_ID --content-stdin
    python3 scripts/log_message.py --role user --session SESSION_ID --content-stdin --uuid UUID_VALUE
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid as uuid_mod
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "gerrit.db")
TRANSCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "transcripts", "fallback")

CANDIDATE_SCHEMA_VERSION = 1

VALID_DIMENSIONS = {
    "communication_style", "vocabulary_language", "humor_wit",
    "values_opinions", "knowledge_expertise", "emotional_relational",
    "cognitive_decision_making"
}

VALID_SPECIFICITIES = {"vague", "general", "specific", "precise"}
VALID_ACTIONS = {"new", "confirm", "contradict", "uncertain"}

DISCLOSURE_TEXT = "[AI SIMULATION — not the real Gerrit] "


def get_connection(db_path=None):
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def retry_on_busy(func, max_retries=5, base_delay=0.1):
    """Retry a function on SQLITE_BUSY errors with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return func()
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
            else:
                raise


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def ensure_session(conn, session_id):
    """Auto-create session if it doesn't exist. Resets safety flags on creation.

    Uses INSERT OR IGNORE to handle concurrent creation races safely.
    """
    mode = get_meta(conn, "current_mode") or "interview"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cur = conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, started_at, mode) VALUES (?, ?, ?)",
        (session_id, now, mode)
    )

    if cur.rowcount > 0:
        # We created the session — reset safety flags
        set_meta(conn, "disclosure_mode", "on")
        set_meta(conn, "impersonation_enabled", "false")

        # Increment total sessions
        total = get_meta(conn, "total_sessions") or "0"
        set_meta(conn, "total_sessions", str(int(total) + 1))

        conn.commit()
        return True
    return False


def write_fallback(session_id, msg_uuid, role, timestamp, content):
    """Write length-prefixed record to fallback transcript."""
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    filepath = os.path.join(TRANSCRIPTS_DIR, f"{session_id}.txt")
    content_bytes = content.encode("utf-8")
    byte_length = len(content_bytes)
    header = f"GERRIT_RECORD {msg_uuid} {role} {timestamp} {byte_length}\n"
    is_new = not os.path.exists(filepath)

    with open(filepath, "ab") as f:
        f.write(header.encode("utf-8"))
        f.write(content_bytes)
        f.write(b"\n")

    if is_new:
        try:
            os.chmod(filepath, 0o600)
        except OSError:
            pass


def validate_candidate(candidate_json_str):
    """Validate observation candidate against schema. Returns (parsed_dict, errors)."""
    errors = []

    try:
        data = json.loads(candidate_json_str)
    except json.JSONDecodeError as e:
        return None, [f"Invalid JSON: {e}"]

    if not isinstance(data, dict):
        return None, ["Candidate must be a JSON object"]

    # Required fields
    required = ["dimension", "facet", "trait", "value", "specificity", "action"]
    for field in required:
        if field not in data:
            errors.append(f"Missing required field: {field}")

    if errors:
        return data, errors

    # Dimension enum
    if data["dimension"] not in VALID_DIMENSIONS:
        errors.append(f"Invalid dimension '{data['dimension']}'. Must be one of: {', '.join(sorted(VALID_DIMENSIONS))}")

    # Facet length
    if not isinstance(data["facet"], str) or len(data["facet"]) > 50:
        errors.append("facet must be a string of max 50 chars")

    # Trait length
    if not isinstance(data["trait"], str) or len(data["trait"]) > 50:
        errors.append("trait must be a string of max 50 chars")

    # Value length
    if not isinstance(data["value"], str) or len(data["value"]) > 500:
        errors.append("value must be a string of max 500 chars")

    # Specificity enum
    if data["specificity"] not in VALID_SPECIFICITIES:
        errors.append(f"Invalid specificity '{data['specificity']}'. Must be one of: {', '.join(sorted(VALID_SPECIFICITIES))}")

    # Action enum
    if data["action"] not in VALID_ACTIONS:
        errors.append(f"Invalid action '{data['action']}'. Must be one of: {', '.join(sorted(VALID_ACTIONS))}")

    # observation_id required for confirm/contradict
    if data.get("action") in ("confirm", "contradict"):
        if "observation_id" not in data or not isinstance(data["observation_id"], int):
            errors.append(f"observation_id (int) is required for action '{data['action']}'")

    # source_message_ids must be array of ints if present
    if "source_message_ids" in data:
        if not isinstance(data["source_message_ids"], list) or not all(isinstance(x, int) for x in data["source_message_ids"]):
            errors.append("source_message_ids must be an array of integers")

    # context length
    if "context" in data:
        if not isinstance(data["context"], str) or len(data["context"]) > 200:
            errors.append("context must be a string of max 200 chars")

    return data, errors


def log_message(role, session_id, content, msg_uuid=None, db_path=None):
    """Log a message to DB and fallback transcript. Returns (message_id, uuid)."""
    conn = get_connection(db_path)
    msg_uuid = msg_uuid or str(uuid_mod.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        ensure_session(conn, session_id)

        # Impersonation guard for assistant messages
        if role == "assistant":
            mode = get_meta(conn, "current_mode")
            if mode == "impersonation":
                imp_enabled = get_meta(conn, "impersonation_enabled")
                if imp_enabled != "true":
                    conn.close()
                    return None, None, "Impersonation not enabled. Set meta.impersonation_enabled='true' to allow."

                disclosure = get_meta(conn, "disclosure_mode")
                if disclosure == "on":
                    content = DISCLOSURE_TEXT + content

                set_meta(conn, "last_impersonation_check", now)

        def do_insert():
            conn.execute(
                "INSERT INTO messages (uuid, session_id, role, content, timestamp, logged_via) VALUES (?, ?, ?, ?, ?, 'primary')",
                (msg_uuid, session_id, role, content, now)
            )
            conn.commit()

        retry_on_busy(do_insert)

        msg_id = conn.execute("SELECT id FROM messages WHERE uuid = ?", (msg_uuid,)).fetchone()[0]

        # Write fallback
        write_fallback(session_id, msg_uuid, role, now, content)

        conn.close()
        return msg_id, msg_uuid, None

    except sqlite3.IntegrityError as e:
        conn.close()
        if "UNIQUE constraint failed: messages.uuid" in str(e):
            return None, msg_uuid, "Duplicate UUID — message already logged"
        raise


def log_candidate(session_id, candidate_json_str, db_path=None):
    """Validate and store an observation candidate. Returns (candidate_id, errors)."""
    data, errors = validate_candidate(candidate_json_str)
    if errors:
        return None, errors

    conn = get_connection(db_path)
    try:
        ensure_session(conn, session_id)

        def do_insert():
            conn.execute(
                "INSERT INTO pending_observations (session_id, candidate_json) VALUES (?, ?)",
                (session_id, json.dumps(data, sort_keys=True))
            )
            conn.commit()

        retry_on_busy(do_insert)

        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return cid, None

    except Exception as e:
        conn.close()
        return None, [str(e)]


def main():
    parser = argparse.ArgumentParser(description="Log messages for Gerrit Digital Twin")
    parser.add_argument("--role", choices=["user", "assistant"], help="Message role")
    parser.add_argument("--session", required=True, help="Session ID")
    parser.add_argument("--content-stdin", action="store_true", help="Read content from stdin")
    parser.add_argument("--uuid", help="Supply UUID externally (for testing/replay)")
    parser.add_argument("--observation-candidate", action="store_true", help="Log observation candidate instead of message")
    parser.add_argument("--db", help="Override database path")

    args = parser.parse_args()

    if not args.content_stdin:
        print(json.dumps({"error": "--content-stdin is required"}))
        sys.exit(1)

    content = sys.stdin.read()

    if args.observation_candidate:
        cid, errors = log_candidate(args.session, content, db_path=args.db)
        if errors:
            print(json.dumps({"error": "Validation failed", "details": errors}))
            sys.exit(1)
        else:
            print(json.dumps({"candidate_id": cid}))
    else:
        if not args.role:
            print(json.dumps({"error": "--role is required for message logging"}))
            sys.exit(1)

        # Validate UUID format if supplied
        msg_uuid = args.uuid
        if msg_uuid:
            try:
                uuid_mod.UUID(msg_uuid)
            except ValueError:
                print(json.dumps({"error": f"Invalid UUID format: {msg_uuid}"}))
                sys.exit(1)

        msg_id, msg_uuid, error = log_message(args.role, args.session, content, msg_uuid=msg_uuid, db_path=args.db)
        if error:
            print(json.dumps({"error": error}))
            sys.exit(1)
        else:
            print(json.dumps({"message_id": msg_id, "uuid": msg_uuid}))


if __name__ == "__main__":
    main()
