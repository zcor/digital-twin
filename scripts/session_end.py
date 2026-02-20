#!/usr/bin/env python3
"""End-of-session processing for Gerrit Digital Twin.

1. Reconciles fallback transcript with DB (UUID-based dedup)
2. Processes pending observation candidates
3. Recalculates confidence scores
4. Regenerates model files
5. Writes session summary
6. Updates session row
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "gerrit.db")
TRANSCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "transcripts")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def get_connection(db_path=None):
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def retry_on_busy(func, max_retries=5, base_delay=0.1):
    for attempt in range(max_retries):
        try:
            return func()
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
            else:
                raise


def parse_fallback_file(filepath):
    """Parse length-prefixed fallback transcript. Returns list of records."""
    records = []
    if not os.path.exists(filepath):
        return records

    with open(filepath, "rb") as f:
        while True:
            header_line = f.readline()
            if not header_line:
                break

            header = header_line.decode("utf-8").rstrip("\n")
            if not header.startswith("GERRIT_RECORD "):
                continue

            parts = header.split(" ", 5)
            if len(parts) < 5:
                continue

            _, msg_uuid, role, timestamp, byte_length_str = parts[:5]
            try:
                byte_length = int(byte_length_str)
            except ValueError:
                continue

            content_bytes = f.read(byte_length)
            content = content_bytes.decode("utf-8")

            # Read trailing newline
            f.read(1)

            records.append({
                "uuid": msg_uuid,
                "role": role,
                "timestamp": timestamp,
                "content": content,
            })

    return records


def reconcile(conn, session_id):
    """Upsert fallback records into DB by UUID."""
    fallback_path = os.path.join(TRANSCRIPTS_DIR, "fallback", f"{session_id}.txt")
    records = parse_fallback_file(fallback_path)
    reconciled = 0

    for rec in records:
        existing = conn.execute("SELECT id FROM messages WHERE uuid = ?", (rec["uuid"],)).fetchone()
        if existing is None:
            def do_insert(r=rec):
                conn.execute(
                    "INSERT INTO messages (uuid, session_id, role, content, timestamp, logged_via) VALUES (?, ?, ?, ?, ?, 'reconciled')",
                    (r["uuid"], session_id, r["role"], r["content"], r["timestamp"])
                )
            retry_on_busy(do_insert)
            reconciled += 1

    conn.commit()
    return reconciled


def process_candidates(conn, session_id):
    """Process pending observation candidates for this session."""
    candidates = conn.execute(
        "SELECT id, candidate_json FROM pending_observations WHERE session_id = ? AND status = 'pending'",
        (session_id,)
    ).fetchall()

    new_count = 0
    updated_count = 0
    results = []

    for cand in candidates:
        try:
            data = json.loads(cand["candidate_json"])
            action = data["action"]
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            if action == "new":
                conn.execute("""
                    INSERT INTO observations (dimension, facet, trait, value, confidence, evidence_count,
                                              specificity, first_observed, last_confirmed, created_by)
                    VALUES (?, ?, ?, ?, 0.3, 1, ?, ?, ?, 'claude')
                """, (data["dimension"], data["facet"], data["trait"], data["value"],
                      data["specificity"], now, now))
                obs_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                # Link to source messages if provided
                if "source_message_ids" in data:
                    for mid in data["source_message_ids"]:
                        conn.execute(
                            "INSERT OR IGNORE INTO observation_messages (observation_id, message_id, relation_type) VALUES (?, ?, 'source')",
                            (obs_id, mid)
                        )

                new_count += 1
                conn.execute("UPDATE pending_observations SET status = 'accepted' WHERE id = ?", (cand["id"],))
                results.append({"candidate_id": cand["id"], "action": "new", "observation_id": obs_id})

            elif action == "confirm":
                obs_id = data["observation_id"]
                existing = conn.execute("SELECT id, evidence_count FROM observations WHERE id = ?", (obs_id,)).fetchone()
                if existing:
                    conn.execute("""
                        UPDATE observations SET evidence_count = evidence_count + 1, last_confirmed = ?
                        WHERE id = ?
                    """, (now, obs_id))

                    if "source_message_ids" in data:
                        for mid in data["source_message_ids"]:
                            conn.execute(
                                "INSERT OR IGNORE INTO observation_messages (observation_id, message_id, relation_type) VALUES (?, ?, 'confirmation')",
                                (obs_id, mid)
                            )

                    updated_count += 1
                    conn.execute("UPDATE pending_observations SET status = 'accepted' WHERE id = ?", (cand["id"],))
                    results.append({"candidate_id": cand["id"], "action": "confirm", "observation_id": obs_id})
                else:
                    conn.execute("UPDATE pending_observations SET status = 'rejected' WHERE id = ?", (cand["id"],))
                    results.append({"candidate_id": cand["id"], "action": "confirm", "error": f"Observation {obs_id} not found"})

            elif action == "contradict":
                obs_id = data["observation_id"]
                existing = conn.execute("SELECT id FROM observations WHERE id = ?", (obs_id,)).fetchone()
                if existing:
                    # Create contradiction link
                    # First, we create the new observation for the contradiction
                    conn.execute("""
                        INSERT INTO observations (dimension, facet, trait, value, confidence, evidence_count,
                                                  specificity, first_observed, last_confirmed, created_by)
                        VALUES (?, ?, ?, ?, 0.3, 1, ?, ?, ?, 'claude')
                    """, (data["dimension"], data["facet"], data["trait"], data["value"],
                          data["specificity"], now, now))
                    new_obs_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                    # Link as contradiction
                    context = data.get("context", "")
                    conn.execute(
                        "INSERT OR IGNORE INTO observation_links (from_observation_id, to_observation_id, link_type, context) VALUES (?, ?, 'contradicts', ?)",
                        (new_obs_id, obs_id, context)
                    )

                    # If context provided, create contextualizing observation
                    if context:
                        conn.execute(
                            "INSERT OR IGNORE INTO observation_links (from_observation_id, to_observation_id, link_type, context) VALUES (?, ?, 'contextualizes', ?)",
                            (new_obs_id, obs_id, context)
                        )

                    if "source_message_ids" in data:
                        for mid in data["source_message_ids"]:
                            conn.execute(
                                "INSERT OR IGNORE INTO observation_messages (observation_id, message_id, relation_type) VALUES (?, ?, 'contradiction')",
                                (new_obs_id, mid)
                            )

                    new_count += 1
                    conn.execute("UPDATE pending_observations SET status = 'accepted' WHERE id = ?", (cand["id"],))
                    results.append({"candidate_id": cand["id"], "action": "contradict", "new_observation_id": new_obs_id, "contradicts": obs_id})
                else:
                    conn.execute("UPDATE pending_observations SET status = 'rejected' WHERE id = ?", (cand["id"],))
                    results.append({"candidate_id": cand["id"], "action": "contradict", "error": f"Observation {obs_id} not found"})

            elif action == "uncertain":
                # Add to gaps table
                conn.execute("""
                    INSERT INTO gaps (dimension, facet, description, priority)
                    VALUES (?, ?, ?, 0.5)
                """, (data["dimension"], data.get("facet"), data["value"]))
                conn.execute("UPDATE pending_observations SET status = 'accepted' WHERE id = ?", (cand["id"],))
                results.append({"candidate_id": cand["id"], "action": "uncertain", "added_to_gaps": True})

        except Exception as e:
            conn.execute("UPDATE pending_observations SET status = 'rejected' WHERE id = ?", (cand["id"],))
            results.append({"candidate_id": cand["id"], "error": str(e)})

    conn.commit()
    return new_count, updated_count, results


def write_session_summary(session_id, reconciled, new_obs, updated_obs, candidate_results, conn):
    """Write session summary markdown."""
    summaries_dir = os.path.join(TRANSCRIPTS_DIR, "summaries")
    os.makedirs(summaries_dir, exist_ok=True)

    msg_count = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]
    session = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary_lines = [
        f"# Session Summary: {session_id}",
        f"",
        f"**Started**: {session['started_at']}",
        f"**Ended**: {now}",
        f"**Mode**: {session['mode']}",
        f"",
        f"## Statistics",
        f"- Messages logged: {msg_count}",
        f"- Messages reconciled from fallback: {reconciled}",
        f"- New observations: {new_obs}",
        f"- Updated observations: {updated_obs}",
        f"",
        f"## Candidate Processing Results",
    ]

    for r in candidate_results:
        summary_lines.append(f"- {json.dumps(r)}")

    summary_text = "\n".join(summary_lines) + "\n"

    filepath = os.path.join(summaries_dir, f"{session_id}.md")
    with open(filepath, "w") as f:
        f.write(summary_text)

    return summary_text


def end_session(session_id, db_path=None):
    """Run full end-of-session processing."""
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Verify session exists
    session = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if not session:
        conn.close()
        print(json.dumps({"error": f"Session {session_id} not found"}))
        return False

    print(f"Processing session: {session_id}")

    # 1. Reconciliation
    reconciled = reconcile(conn, session_id)
    print(f"  Reconciled {reconciled} messages from fallback")

    # 2. Process candidates
    new_obs, updated_obs, candidate_results = process_candidates(conn, session_id)
    print(f"  New observations: {new_obs}, Updated: {updated_obs}")

    # 3. Write session summary
    summary = write_session_summary(session_id, reconciled, new_obs, updated_obs, candidate_results, conn)

    # 4. Update session row
    conn.execute("""
        UPDATE sessions SET ended_at = ?, new_observations = ?, updated_observations = ?,
                           session_summary = ?
        WHERE session_id = ?
    """, (now, new_obs, updated_obs, summary, session_id))
    conn.commit()
    conn.close()

    # 5. Regenerate model (confidence recalculated inside export_model)
    export_script = os.path.join(SCRIPTS_DIR, "export_model.py")
    db_arg = ["--db", db_path] if db_path else []
    subprocess.run([sys.executable, export_script] + db_arg, check=True)
    print("  Model regenerated")

    return True


def main():
    parser = argparse.ArgumentParser(description="End-of-session processing")
    parser.add_argument("--session", required=True, help="Session ID to finalize")
    parser.add_argument("--db", help="Override database path")

    args = parser.parse_args()
    success = end_session(args.session, db_path=args.db)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
