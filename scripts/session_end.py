#!/usr/bin/env python3
"""End-of-session processing for Gerrit Digital Twin.

1. Reconciles fallback transcript with DB (UUID-based dedup)
2. Processes pending observation candidates
3. Recalculates confidence scores
4. Regenerates model files
5. Writes session summary
6. Updates session row

Supports token+heartbeat claim protocol for safe concurrent finalization.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone

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


def process_candidates(conn, session_id, heartbeat_fn=None):
    """Process pending observation candidates for this session.

    If heartbeat_fn is provided, it is called after each candidate. If it
    returns False (claim lost), processing stops early and returns partial results.

    Returns (new_count, updated_count, vocab_count, exemplar_count, results).
    new_count/updated_count are observation-only. vocab_count/exemplar_count are separate.
    """
    candidates = conn.execute(
        "SELECT id, candidate_json FROM pending_observations WHERE session_id = ? AND status = 'pending'",
        (session_id,)
    ).fetchall()

    new_count = 0       # observations only
    updated_count = 0   # observations only
    vocab_count = 0     # vocabulary entries
    exemplar_count = 0  # exemplar entries
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
                    conn.execute("""
                        INSERT INTO observations (dimension, facet, trait, value, confidence, evidence_count,
                                                  specificity, first_observed, last_confirmed, created_by)
                        VALUES (?, ?, ?, ?, 0.3, 1, ?, ?, ?, 'claude')
                    """, (data["dimension"], data["facet"], data["trait"], data["value"],
                          data["specificity"], now, now))
                    new_obs_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                    context = data.get("context", "")
                    conn.execute(
                        "INSERT OR IGNORE INTO observation_links (from_observation_id, to_observation_id, link_type, context) VALUES (?, ?, 'contradicts', ?)",
                        (new_obs_id, obs_id, context)
                    )

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
                conn.execute("""
                    INSERT INTO gaps (dimension, facet, description, priority)
                    VALUES (?, ?, ?, 0.5)
                """, (data["dimension"], data.get("facet"), data["value"]))
                conn.execute("UPDATE pending_observations SET status = 'accepted' WHERE id = ?", (cand["id"],))
                results.append({"candidate_id": cand["id"], "action": "uncertain", "added_to_gaps": True})

            elif action == "vocabulary":
                term = data["term"]
                normalized = term.lower().strip()
                category = data["category"]
                context = data.get("context", "")
                frequency = data.get("frequency", "occasional")

                existing = conn.execute(
                    "SELECT id FROM vocabulary WHERE normalized_term = ?", (normalized,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE vocabulary SET session_count = session_count + 1 WHERE id = ?",
                        (existing[0],)
                    )
                    vocab_count += 1
                    conn.execute("UPDATE pending_observations SET status = 'accepted' WHERE id = ?", (cand["id"],))
                    results.append({"candidate_id": cand["id"], "action": "vocabulary", "term": term, "updated": True})
                else:
                    conn.execute("""
                        INSERT INTO vocabulary (term, normalized_term, category, frequency, context, first_observed, session_count)
                        VALUES (?, ?, ?, ?, ?, ?, 1)
                    """, (term, normalized, category, frequency, context, now))
                    vocab_count += 1
                    conn.execute("UPDATE pending_observations SET status = 'accepted' WHERE id = ?", (cand["id"],))
                    results.append({"candidate_id": cand["id"], "action": "vocabulary", "term": term, "new": True})

            elif action == "exemplar":
                obs_id = data["observation_id"]
                quote = data["quote"]
                context = data.get("context", "")
                significance = data.get("significance", "")
                source_msg_id = data["source_message_ids"][0]

                existing_obs = conn.execute("SELECT id FROM observations WHERE id = ?", (obs_id,)).fetchone()
                existing_msg = conn.execute("SELECT id FROM messages WHERE id = ?", (source_msg_id,)).fetchone()
                if not existing_obs or not existing_msg:
                    conn.execute("UPDATE pending_observations SET status = 'rejected' WHERE id = ?", (cand["id"],))
                    results.append({"candidate_id": cand["id"], "action": "exemplar",
                                    "error": f"Observation {obs_id} or message {source_msg_id} not found"})
                else:
                    # Dedupe: skip if exact (observation_id, source_message_id, quote) already exists
                    dupe = conn.execute("""
                        SELECT id FROM exemplars
                        WHERE observation_id = ? AND source_message_id = ? AND quote = ?
                    """, (obs_id, source_msg_id, quote)).fetchone()
                    if dupe:
                        conn.execute("UPDATE pending_observations SET status = 'accepted' WHERE id = ?", (cand["id"],))
                        results.append({"candidate_id": cand["id"], "action": "exemplar", "duplicate": True})
                    else:
                        conn.execute("""
                            INSERT INTO exemplars (observation_id, source_message_id, quote, context, significance)
                            VALUES (?, ?, ?, ?, ?)
                        """, (obs_id, source_msg_id, quote, context, significance))
                        exemplar_count += 1
                        conn.execute("UPDATE pending_observations SET status = 'accepted' WHERE id = ?", (cand["id"],))
                        results.append({"candidate_id": cand["id"], "action": "exemplar", "observation_id": obs_id})

        except Exception as e:
            conn.execute("UPDATE pending_observations SET status = 'rejected' WHERE id = ?", (cand["id"],))
            results.append({"candidate_id": cand["id"], "error": str(e)})

        conn.commit()

        # Heartbeat after each candidate
        if heartbeat_fn is not None:
            if not heartbeat_fn():
                # Claim lost — stop processing, return partial results
                break

    return new_count, updated_count, vocab_count, exemplar_count, results


def extract_topics(session_id, candidate_results, conn):
    """Extract topics_covered as a JSON array of snake_case dimension keys.

    Primary: parse dimensions from accepted candidates for this session.
    Fallback: keyword heuristic from user messages.
    """
    # Primary: parse dimensions from accepted candidates
    accepted_ids = [r["candidate_id"] for r in candidate_results
                    if r.get("action") in ("new", "confirm", "contradict", "uncertain")]
    topics = []
    if accepted_ids:
        placeholders = ",".join("?" * len(accepted_ids))
        rows = conn.execute(
            f"SELECT candidate_json FROM pending_observations WHERE id IN ({placeholders})",
            accepted_ids,
        ).fetchall()
        dims = set()
        for row in rows:
            data = json.loads(row[0])
            dims.add(data["dimension"])
        topics = sorted(dims)

    # Fallback: keyword heuristic from user messages
    if not topics:
        user_msgs = conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user'",
            (session_id,),
        ).fetchall()
        if user_msgs:
            all_text = " ".join(m[0].lower() for m in user_msgs)
            dimension_keywords = {
                "communication_style": ["text", "write", "say", "talk", "message", "email"],
                "vocabulary_language": ["word", "phrase", "slang", "jargon", "language"],
                "humor_wit": ["joke", "funny", "humor", "laugh", "sarcas"],
                "values_opinions": ["believe", "value", "opinion", "important", "matter"],
                "knowledge_expertise": ["work", "career", "hobby", "skill", "know"],
                "emotional_relational": ["feel", "friend", "relationship", "stress", "emotion"],
                "cognitive_decision_making": ["decide", "think", "reason", "risk", "plan"],
            }
            for dim, keywords in dimension_keywords.items():
                if any(kw in all_text for kw in keywords):
                    topics.append(dim)

    return topics


def write_session_summary(session_id, reconciled, new_obs, updated_obs, vocab_count, exemplar_count, candidate_results, conn):
    """Write session summary markdown and populate topics_covered."""
    summaries_dir = os.path.join(TRANSCRIPTS_DIR, "summaries")
    os.makedirs(summaries_dir, exist_ok=True)

    msg_count = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]
    session = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()

    # Extract and store topics
    topics = extract_topics(session_id, candidate_results, conn)
    conn.execute("UPDATE sessions SET topics_covered = ? WHERE session_id = ?",
                 (json.dumps(topics), session_id))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Determine logging status (display-only, not stored in DB)
    if msg_count > 0:
        logging_status = "complete"
    elif session["mode"] == "impersonation":
        logging_status = "potential_logging_failure"
    else:
        logging_status = "empty_session"

    summary_lines = [
        f"# Session Summary: {session_id}",
        f"",
        f"**Started**: {session['started_at']}",
        f"**Ended**: {now}",
        f"**Mode**: {session['mode']}",
        f"**Logging status**: {logging_status}",
        f"",
        f"## Statistics",
        f"- Messages logged: {msg_count}",
        f"- Messages reconciled from fallback: {reconciled}",
        f"- New observations: {new_obs}",
        f"- Updated observations: {updated_obs}",
        f"- Vocabulary entries: {vocab_count}",
        f"- Exemplar quotes: {exemplar_count}",
        f"",
        f"## Candidate Processing Results",
    ]

    for r in candidate_results:
        summary_lines.append(f"- {json.dumps(r)}")

    if topics:
        summary_lines.append("")
        summary_lines.append("## Topics")
        for t in topics:
            summary_lines.append(f"- {t}")

    summary_text = "\n".join(summary_lines) + "\n"

    filepath = os.path.join(summaries_dir, f"{session_id}.md")
    with open(filepath, "w") as f:
        f.write(summary_text)

    return summary_text


def heartbeat_claim(conn, session_id, token):
    """Update finalizing_at to prove liveness. Returns False if our claim was stolen."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute("""
        UPDATE sessions SET finalizing_at = ?
        WHERE session_id = ? AND finalizing_token = ?
    """, (now, session_id, token))
    conn.commit()
    return cur.rowcount > 0


def end_session(session_id, db_path=None):
    """Run full end-of-session processing with token+heartbeat claim.

    Returns (success: bool, reason: str).
    """
    conn = get_connection(db_path)
    token = str(uuid_mod.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Atomic claim: only if not currently claimed and not already ended
    cur = conn.execute("""
        UPDATE sessions SET finalizing_token = ?, finalizing_at = ?
        WHERE session_id = ? AND ended_at IS NULL AND finalizing_token IS NULL
    """, (token, now, session_id))
    conn.commit()

    if cur.rowcount == 0:
        session = conn.execute(
            "SELECT session_id, ended_at, finalizing_token FROM sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        conn.close()
        if not session:
            return False, "not found"
        if session["ended_at"] is not None:
            return True, "already finalized"
        if session["finalizing_token"] is not None:
            return True, "claimed by another worker"
        return False, "unknown"

    # Verify our token stuck
    actual = conn.execute(
        "SELECT finalizing_token FROM sessions WHERE session_id = ?",
        (session_id,)
    ).fetchone()[0]
    if actual != token:
        conn.close()
        return True, "claim lost"

    # We own it. Process with heartbeats between steps.
    try:
        print(f"Processing session: {session_id}")

        # 1. Reconciliation
        reconciled = reconcile(conn, session_id)
        print(f"  Reconciled {reconciled} messages from fallback")

        # Check for zero-message sessions after reconciliation
        post_recon_msg_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        session_row = conn.execute(
            "SELECT mode, impersonation_enabled FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if post_recon_msg_count == 0 and session_row["mode"] == "impersonation":
            print(f"\n  WARNING: Impersonation session {session_id} has ZERO messages logged.")
            print(f"  This likely means logging was skipped during the session.")
            print(f"  To import a transcript retroactively:")
            print(f"    python3 scripts/import_transcript.py --session {session_id} --content-stdin")
            print()

        if not heartbeat_claim(conn, session_id, token):
            conn.close()
            return False, "claim lost during reconciliation"

        # 2. Process candidates with heartbeat callback
        def heartbeat():
            return heartbeat_claim(conn, session_id, token)

        new_obs, updated_obs, vocab_count, exemplar_count, candidate_results = process_candidates(
            conn, session_id, heartbeat_fn=heartbeat)
        print(f"  New observations: {new_obs}, Updated: {updated_obs}, Vocab: {vocab_count}, Exemplars: {exemplar_count}")

        if not heartbeat_claim(conn, session_id, token):
            conn.close()
            return False, "claim lost during candidate processing"

        # 3. Write session summary
        summary = write_session_summary(session_id, reconciled, new_obs, updated_obs, vocab_count, exemplar_count, candidate_results, conn)

        # 4. Finalize: set real ended_at, clear claim — guarded by our token
        final_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = conn.execute("""
            UPDATE sessions SET ended_at = ?, new_observations = ?,
                               updated_observations = ?, session_summary = ?,
                               finalizing_token = NULL, finalizing_at = NULL
            WHERE session_id = ? AND finalizing_token = ?
        """, (final_now, new_obs, updated_obs, summary, session_id, token))
        conn.commit()
        conn.close()

        # Check if our finalization actually wrote (token might have been stolen)
        if cur.rowcount == 0:
            return False, "claim lost before finalization"

        # 5. Only regenerate model if we successfully finalized
        export_script = os.path.join(SCRIPTS_DIR, "export_model.py")
        db_arg = ["--db", db_path] if db_path else []
        subprocess.run([sys.executable, export_script] + db_arg, check=True)
        print("  Model regenerated")

        # 6. Regenerate system prompt for twin chatbot
        export_prompt_script = os.path.join(SCRIPTS_DIR, "export_prompt.py")
        if os.path.exists(export_prompt_script):
            subprocess.run([sys.executable, export_prompt_script], check=False)
            print("  System prompt regenerated")

        return True, "finalized"

    except Exception as e:
        # Leave claim columns intact — timeout reclaim will pick this up
        try:
            conn.close()
        except Exception:
            pass
        return False, f"processing error (claim retained for reclaim): {e}"


def finalize_stale_sessions(db_path=None, max_age_minutes=30, claim_timeout_minutes=2):
    """Find and finalize orphaned sessions.

    Two paths:
    1. Unclaimed stale sessions (no active claim, last message old)
    2. Crashed claims (token set, heartbeat stale)
    """
    conn = get_connection(db_path)
    msg_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    claim_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=claim_timeout_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Path 1: Unclaimed stale sessions
    unclaimed = conn.execute("""
        SELECT s.session_id FROM sessions s
        WHERE s.ended_at IS NULL AND s.finalizing_token IS NULL
        AND s.started_at < ?
        AND (
            NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.session_id)
            OR (SELECT MAX(m.timestamp) FROM messages m
                WHERE m.session_id = s.session_id) < ?
        )
        ORDER BY s.started_at ASC
    """, (msg_cutoff, msg_cutoff)).fetchall()

    # Path 2: Crashed claims (token set, ended_at IS NULL, heartbeat stale)
    crashed = conn.execute("""
        SELECT s.session_id FROM sessions s
        WHERE s.ended_at IS NULL
        AND s.finalizing_token IS NOT NULL
        AND s.finalizing_at < ?
        ORDER BY s.started_at ASC
    """, (claim_cutoff,)).fetchall()

    conn.close()

    results = []

    # Normal claim path for unclaimed sessions
    for row in unclaimed:
        success, reason = end_session(row["session_id"], db_path=db_path)
        results.append({"session_id": row["session_id"], "success": success, "reason": reason})

    # Reclaim crashed sessions: clear stale token (guarded by ended_at IS NULL + stale age)
    for row in crashed:
        conn2 = get_connection(db_path)
        conn2.execute("""
            UPDATE sessions SET finalizing_token = NULL, finalizing_at = NULL
            WHERE session_id = ? AND ended_at IS NULL AND finalizing_at < ?
        """, (row["session_id"], claim_cutoff))
        conn2.commit()
        conn2.close()

        success, reason = end_session(row["session_id"], db_path=db_path)
        results.append({"session_id": row["session_id"], "success": success,
                        "reason": f"reclaimed: {reason}"})

    output = {"stale_sessions": len(unclaimed) + len(crashed), "results": results}
    print(json.dumps(output))
    return results


def main():
    parser = argparse.ArgumentParser(description="End-of-session processing")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", help="Session ID to finalize")
    group.add_argument("--all-stale", action="store_true", help="Finalize all stale sessions")
    parser.add_argument("--max-age", type=int, default=30, help="Minutes since last message (default 30)")
    parser.add_argument("--claim-timeout", type=int, default=2, help="Minutes before reclaiming stale claim (default 2)")
    parser.add_argument("--db", help="Override database path")

    args = parser.parse_args()

    if args.session:
        success, reason = end_session(args.session, db_path=args.db)
        print(json.dumps({"session_id": args.session, "success": success, "reason": reason}))
        if not success and reason != "not found":
            sys.exit(0)
        elif not success:
            sys.exit(1)
    else:
        finalize_stale_sessions(
            db_path=args.db,
            max_age_minutes=args.max_age,
            claim_timeout_minutes=args.claim_timeout,
        )


if __name__ == "__main__":
    main()
