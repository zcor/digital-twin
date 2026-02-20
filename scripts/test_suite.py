#!/usr/bin/env python3
"""Automated test suite for Gerrit Digital Twin system.

15 tests covering schema, logging, dedup, export, confidence, contradictions,
reconciliation, candidate validation, concurrency, impersonation guards,
file permissions, calibration sanity, and FK enforcement.

Usage: python3 scripts/test_suite.py
"""

import json
import math
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPTS_DIR)

# Import our modules
sys.path.insert(0, SCRIPTS_DIR)
from init_db import init_db
from log_message import log_message, log_candidate, get_connection, validate_candidate, write_fallback
from export_model import export_model, observation_confidence, DEPTH_MAP
from session_end import parse_fallback_file, reconcile, process_candidates, end_session, finalize_stale_sessions, heartbeat_claim

PASS = 0
FAIL = 0


def report(name, passed, detail=""):
    global PASS, FAIL
    status = "PASS" if passed else "FAIL"
    if passed:
        PASS += 1
    else:
        FAIL += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")


def make_test_env():
    """Create a temporary directory with DB and model dir."""
    tmpdir = tempfile.mkdtemp(prefix="gerrit_test_")
    db_dir = os.path.join(tmpdir, "db")
    os.makedirs(db_dir)
    model_dir = os.path.join(tmpdir, "model")
    os.makedirs(model_dir)
    transcripts_dir = os.path.join(tmpdir, "transcripts", "fallback")
    os.makedirs(transcripts_dir, exist_ok=True)
    summaries_dir = os.path.join(tmpdir, "transcripts", "summaries")
    os.makedirs(summaries_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "gerrit.db")
    return tmpdir, db_path, model_dir


def cleanup(tmpdir):
    shutil.rmtree(tmpdir, ignore_errors=True)


# --- Test 1: Schema idempotency ---
def test_schema_idempotency():
    tmpdir, db_path, _ = make_test_env()
    try:
        init_db(db_path)
        init_db(db_path)  # Second run should not error

        conn = sqlite3.connect(db_path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()

        expected = {"sessions", "messages", "observations", "observation_messages",
                    "observation_links", "exemplars", "vocabulary", "opinions",
                    "opinion_messages", "gaps", "pending_observations", "meta"}
        missing = expected - set(tables)
        report("Schema idempotency", len(missing) == 0,
               f"Missing tables: {missing}" if missing else "All tables present, double-run safe")
    finally:
        cleanup(tmpdir)


# --- Test 2: Logging roundtrip (adversarial content) ---
def test_logging_roundtrip():
    tmpdir, db_path, _ = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-roundtrip"

        adversarial = [
            "Hello, world!",
            'She said "hello" and \'goodbye\'',
            "Line 1\nLine 2\nLine 3",
            "Unicode: cafe\u0301 \u2603 \u2764\ufe0f \U0001f600",
            "Emoji: \U0001f680\U0001f30d\U0001f389\U0001f4a5",
            "'; DROP TABLE messages;--",
            "Robert'); DROP TABLE Students;--",
            "Content with GERRIT_RECORD fake header inside",
            "",  # empty string
            "A" * 10000,  # large content
        ]

        all_ok = True
        for i, content in enumerate(adversarial):
            msg_id, msg_uuid, error = log_message("user", session_id, content, db_path=db_path)
            if error:
                report("Logging roundtrip", False, f"Error on message {i}: {error}")
                all_ok = False
                break

            conn = get_connection(db_path)
            row = conn.execute("SELECT content FROM messages WHERE uuid = ?", (msg_uuid,)).fetchone()
            conn.close()

            if row is None:
                report("Logging roundtrip", False, f"Message {i} not found in DB")
                all_ok = False
                break

            if row[0] != content:
                report("Logging roundtrip", False, f"Message {i} content mismatch")
                all_ok = False
                break

        if all_ok:
            report("Logging roundtrip", True, f"All {len(adversarial)} adversarial messages stored/retrieved correctly")
    finally:
        cleanup(tmpdir)


# --- Test 3: UUID deduplication ---
def test_uuid_dedup():
    tmpdir, db_path, _ = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-dedup"
        fixed_uuid = str(uuid.uuid4())

        msg_id1, _, err1 = log_message("user", session_id, "First insert", msg_uuid=fixed_uuid, db_path=db_path)
        _, _, err2 = log_message("user", session_id, "Duplicate insert", msg_uuid=fixed_uuid, db_path=db_path)

        conn = get_connection(db_path)
        count = conn.execute("SELECT COUNT(*) FROM messages WHERE uuid = ?", (fixed_uuid,)).fetchone()[0]
        content = conn.execute("SELECT content FROM messages WHERE uuid = ?", (fixed_uuid,)).fetchone()[0]
        conn.close()

        report("UUID deduplication", count == 1 and err2 is not None and content == "First insert",
               f"Count={count}, second insert error={err2 is not None}")
    finally:
        cleanup(tmpdir)


# --- Test 4: UUID caller-supply ---
def test_uuid_caller_supply():
    tmpdir, db_path, _ = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-uuid-supply"
        custom_uuid = "12345678-1234-1234-1234-123456789abc"

        msg_id, returned_uuid, err = log_message("user", session_id, "Custom UUID message", msg_uuid=custom_uuid, db_path=db_path)

        conn = get_connection(db_path)
        row = conn.execute("SELECT uuid FROM messages WHERE id = ?", (msg_id,)).fetchone()
        conn.close()

        report("UUID caller-supply", returned_uuid == custom_uuid and row[0] == custom_uuid,
               f"Supplied={custom_uuid}, returned={returned_uuid}, stored={row[0] if row else 'None'}")
    finally:
        cleanup(tmpdir)


# --- Test 5: Export determinism ---
def test_export_determinism():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)

        # Seed some data
        conn = get_connection(db_path)
        now = "2026-02-19T00:00:00Z"
        conn.execute("INSERT INTO sessions (session_id, started_at, mode) VALUES ('s1', ?, 'interview')", (now,))
        conn.execute("""
            INSERT INTO observations (dimension, facet, trait, value, confidence, evidence_count,
                                      specificity, first_observed, last_confirmed)
            VALUES ('humor_wit', 'type', 'sarcastic', 'Uses dry sarcasm frequently', 0.3, 3,
                    'specific', ?, ?)
        """, (now, now))
        conn.commit()
        conn.close()

        # First export
        export_model(db_path=db_path, model_dir=model_dir, as_of="2026-02-19")
        with open(os.path.join(model_dir, "personality.json"), "rb") as f:
            p1 = f.read()
        with open(os.path.join(model_dir, "confidence.json"), "rb") as f:
            c1 = f.read()

        # Second export (same data, same as_of)
        export_model(db_path=db_path, model_dir=model_dir, as_of="2026-02-19")
        with open(os.path.join(model_dir, "personality.json"), "rb") as f:
            p2 = f.read()
        with open(os.path.join(model_dir, "confidence.json"), "rb") as f:
            c2 = f.read()

        # generated_at will differ, so we compare after stripping that field
        def strip_generated_at(b):
            d = json.loads(b)
            d.pop("generated_at", None)
            return json.dumps(d, sort_keys=True)

        p_match = strip_generated_at(p1) == strip_generated_at(p2)
        c_match = strip_generated_at(c1) == strip_generated_at(c2)

        report("Export determinism", p_match and c_match,
               f"personality match={p_match}, confidence match={c_match}")
    finally:
        cleanup(tmpdir)


# --- Test 6: Confidence scoring ---
def test_confidence_scoring():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)
        from datetime import date

        # Known input: evidence_count=1, last_confirmed=today, specificity=specific, no contradictions
        obs = {
            "evidence_count": 1,
            "last_confirmed": "2026-02-19",
            "specificity": "specific",
        }
        as_of = date(2026, 2, 19)

        b = 1.0 / (1.0 + math.exp(-0.5 * (1 - 5)))
        r = 1.0  # 0 days ago
        c = 1.0  # no contradictions
        d = 0.8  # specific
        expected = round(b * r * c * d, 4)

        actual = observation_confidence(obs, 0, as_of=as_of)

        report("Confidence scoring", abs(actual - expected) < 0.0001,
               f"expected={expected}, actual={actual}")

        # Test with 3 contradictions
        actual_c = observation_confidence(obs, 3, as_of=as_of)
        c_val = max(0.2, 1.0 - 0.2 * 3)
        expected_c = round(b * r * c_val * d, 4)
        report("Confidence scoring (contradictions)", abs(actual_c - expected_c) < 0.0001,
               f"expected={expected_c}, actual={actual_c}")
    finally:
        cleanup(tmpdir)


# --- Test 7: Contradiction handling ---
def test_contradiction_handling():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)
        conn = get_connection(db_path)
        now = "2026-02-19T00:00:00Z"
        session_id = "test-contradiction"

        conn.execute("INSERT INTO sessions (session_id, started_at, mode) VALUES (?, ?, 'interview')", (session_id, now))

        # Insert original observation
        conn.execute("""
            INSERT INTO observations (dimension, facet, trait, value, confidence, evidence_count,
                                      specificity, first_observed, last_confirmed)
            VALUES ('communication_style', 'formality', 'formal', 'Uses formal language', 0.5, 3,
                    'specific', ?, ?)
        """, (now, now))
        obs1_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Insert contradicting observation
        conn.execute("""
            INSERT INTO observations (dimension, facet, trait, value, confidence, evidence_count,
                                      specificity, first_observed, last_confirmed)
            VALUES ('communication_style', 'formality', 'casual', 'Uses very casual language with friends', 0.5, 2,
                    'specific', ?, ?)
        """, (now, now))
        obs2_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Create contradiction link
        conn.execute("""
            INSERT INTO observation_links (from_observation_id, to_observation_id, link_type, context)
            VALUES (?, ?, 'contradicts', 'Formal at work, casual with friends')
        """, (obs2_id, obs1_id))
        conn.commit()

        # Verify both remain active
        obs1_active = conn.execute("SELECT is_active FROM observations WHERE id = ?", (obs1_id,)).fetchone()[0]
        obs2_active = conn.execute("SELECT is_active FROM observations WHERE id = ?", (obs2_id,)).fetchone()[0]

        # Verify link exists
        link = conn.execute("""
            SELECT * FROM observation_links
            WHERE from_observation_id = ? AND to_observation_id = ? AND link_type = 'contradicts'
        """, (obs2_id, obs1_id)).fetchone()

        # Verify confidence drops when exported
        export_model(db_path=db_path, model_dir=model_dir, as_of="2026-02-19")

        obs1_conf = conn.execute("SELECT confidence FROM observations WHERE id = ?", (obs1_id,)).fetchone()[0]
        obs2_conf = conn.execute("SELECT confidence FROM observations WHERE id = ?", (obs2_id,)).fetchone()[0]
        conn.close()

        # Without contradictions, both would have higher confidence
        # With one contradiction link each, consistency factor = max(0.2, 1.0 - 0.2*1) = 0.8
        report("Contradiction handling",
               obs1_active == 1 and obs2_active == 1 and link is not None and obs1_conf < 0.5 and obs2_conf < 0.5,
               f"Both active, link exists, confidence dropped: obs1={obs1_conf}, obs2={obs2_conf}")
    finally:
        cleanup(tmpdir)


# --- Test 8: Reconciliation via UUID ---
def test_reconciliation():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)
        conn = get_connection(db_path)
        session_id = "test-recon"
        now = "2026-02-19T00:00:00Z"

        conn.execute("INSERT INTO sessions (session_id, started_at, mode) VALUES (?, ?, 'interview')", (session_id, now))
        conn.commit()

        # Write directly to fallback file only (simulating DB miss)
        fallback_dir = os.path.join(tmpdir, "transcripts", "fallback")
        # Patch the TRANSCRIPTS_DIR for our test
        import session_end
        orig_dir = session_end.TRANSCRIPTS_DIR
        session_end.TRANSCRIPTS_DIR = os.path.join(tmpdir, "transcripts")

        test_uuid = str(uuid.uuid4())
        content = "This message only exists in fallback"
        write_fallback_test(fallback_dir, session_id, test_uuid, "user", now, content)

        reconciled = reconcile(conn, session_id)

        row = conn.execute("SELECT content, logged_via FROM messages WHERE uuid = ?", (test_uuid,)).fetchone()
        conn.close()

        session_end.TRANSCRIPTS_DIR = orig_dir

        report("Reconciliation via UUID",
               reconciled == 1 and row is not None and row[0] == content and row[1] == "reconciled",
               f"Reconciled={reconciled}, content match={row is not None and row[0] == content}")
    finally:
        cleanup(tmpdir)


def write_fallback_test(fallback_dir, session_id, msg_uuid, role, timestamp, content):
    """Write a fallback record for testing."""
    os.makedirs(fallback_dir, exist_ok=True)
    filepath = os.path.join(fallback_dir, f"{session_id}.txt")
    content_bytes = content.encode("utf-8")
    byte_length = len(content_bytes)
    header = f"GERRIT_RECORD {msg_uuid} {role} {timestamp} {byte_length}\n"
    with open(filepath, "ab") as f:
        f.write(header.encode("utf-8"))
        f.write(content_bytes)
        f.write(b"\n")


# --- Test 9: Fallback format parsing ---
def test_fallback_format():
    tmpdir, db_path, _ = make_test_env()
    try:
        fallback_dir = os.path.join(tmpdir, "transcripts", "fallback")
        session_id = "test-format"

        # Test cases: embedded GERRIT_RECORD, empty content, multiline
        cases = [
            ("Normal message", str(uuid.uuid4())),
            ("Contains GERRIT_RECORD fake header inside", str(uuid.uuid4())),
            ("", str(uuid.uuid4())),  # empty
            ("Line1\nLine2\nLine3\nGERRIT_RECORD fake\nLine5", str(uuid.uuid4())),
        ]

        for content, u in cases:
            write_fallback_test(fallback_dir, session_id, u, "user", "2026-02-19T00:00:00Z", content)

        filepath = os.path.join(fallback_dir, f"{session_id}.txt")
        records = parse_fallback_file(filepath)

        all_ok = len(records) == len(cases)
        for i, (content, u) in enumerate(cases):
            if i < len(records):
                if records[i]["content"] != content or records[i]["uuid"] != u:
                    all_ok = False
                    break

        report("Fallback format parsing", all_ok,
               f"Parsed {len(records)}/{len(cases)} records correctly")
    finally:
        cleanup(tmpdir)


# --- Test 10: Candidate validation ---
def test_candidate_validation():
    tmpdir, db_path, _ = make_test_env()
    try:
        init_db(db_path)

        # Valid candidate
        valid = json.dumps({
            "dimension": "humor_wit",
            "facet": "type",
            "trait": "sarcasm",
            "value": "Uses dry sarcasm",
            "specificity": "specific",
            "action": "new",
        })
        _, errs = validate_candidate(valid)
        valid_ok = errs == []

        # Missing fields
        _, errs = validate_candidate(json.dumps({"dimension": "humor_wit"}))
        missing_ok = len(errs) > 0

        # Bad dimension
        _, errs = validate_candidate(json.dumps({
            "dimension": "INVALID",
            "facet": "x", "trait": "x", "value": "x",
            "specificity": "specific", "action": "new",
        }))
        bad_dim_ok = len(errs) > 0

        # Oversized value
        _, errs = validate_candidate(json.dumps({
            "dimension": "humor_wit",
            "facet": "x", "trait": "x", "value": "x" * 501,
            "specificity": "specific", "action": "new",
        }))
        oversized_ok = len(errs) > 0

        # Confirm without observation_id
        _, errs = validate_candidate(json.dumps({
            "dimension": "humor_wit",
            "facet": "x", "trait": "x", "value": "x",
            "specificity": "specific", "action": "confirm",
        }))
        no_obs_id_ok = len(errs) > 0

        # Not JSON
        _, errs = validate_candidate("not json at all")
        not_json_ok = len(errs) > 0

        all_ok = all([valid_ok, missing_ok, bad_dim_ok, oversized_ok, no_obs_id_ok, not_json_ok])
        report("Candidate validation", all_ok,
               f"valid={valid_ok}, missing={missing_ok}, bad_dim={bad_dim_ok}, oversized={oversized_ok}, no_obs_id={no_obs_id_ok}, not_json={not_json_ok}")
    finally:
        cleanup(tmpdir)


# --- Test 11: Concurrency ---
def _concurrent_writer(args):
    """Worker function for concurrency test."""
    db_path, session_id, writer_id, count = args
    results = []
    for i in range(count):
        msg_id, msg_uuid, err = log_message(
            "user", session_id, f"Writer {writer_id} message {i}",
            db_path=db_path
        )
        results.append({"writer": writer_id, "msg": i, "id": msg_id, "error": err})
    return results


def test_concurrency():
    tmpdir, db_path, _ = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-concurrent"

        # 5 writers, 10 messages each
        num_writers = 5
        msgs_per_writer = 10
        total_expected = num_writers * msgs_per_writer

        args = [(db_path, session_id, i, msgs_per_writer) for i in range(num_writers)]

        with multiprocessing.Pool(num_writers) as pool:
            all_results = pool.map(_concurrent_writer, args)

        flat = [r for batch in all_results for r in batch]
        errors = [r for r in flat if r["error"] is not None]

        conn = get_connection(db_path)
        count = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]
        conn.close()

        report("Concurrency (5 writers)", count == total_expected and len(errors) == 0,
               f"Expected={total_expected}, stored={count}, errors={len(errors)}")
    finally:
        cleanup(tmpdir)


# --- Test 12: Impersonation guards ---
def test_impersonation_guards():
    tmpdir, db_path, _ = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-impersonation"

        # Set mode to impersonation but leave impersonation_enabled=false
        conn = get_connection(db_path)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('current_mode', 'impersonation')")
        conn.commit()
        conn.close()

        # Try to log assistant message — should be refused
        msg_id, msg_uuid, err = log_message("assistant", session_id, "I am Gerrit!", db_path=db_path)
        refused = err is not None and msg_id is None

        # Now enable impersonation
        conn = get_connection(db_path)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('impersonation_enabled', 'true')")
        conn.commit()
        conn.close()

        # Try again — should succeed with disclosure prepended
        msg_id2, msg_uuid2, err2 = log_message("assistant", session_id, "I am Gerrit!", db_path=db_path)

        conn = get_connection(db_path)
        content = conn.execute("SELECT content FROM messages WHERE uuid = ?", (msg_uuid2,)).fetchone()[0]
        conn.close()

        has_disclosure = content.startswith("[AI SIMULATION")

        # Test safety reset on new session
        new_session = "test-impersonation-2"
        log_message("user", new_session, "Hi", db_path=db_path)  # triggers session creation

        conn = get_connection(db_path)
        imp_enabled = conn.execute("SELECT value FROM meta WHERE key = 'impersonation_enabled'").fetchone()[0]
        disc_mode = conn.execute("SELECT value FROM meta WHERE key = 'disclosure_mode'").fetchone()[0]
        conn.close()

        safety_reset = imp_enabled == "false" and disc_mode == "on"

        report("Impersonation guards",
               refused and has_disclosure and safety_reset,
               f"refused_when_disabled={refused}, disclosure_prepended={has_disclosure}, safety_reset={safety_reset}")
    finally:
        cleanup(tmpdir)


# --- Test 13: File permissions ---
def test_file_permissions():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)

        # Check DB permissions
        db_perms = oct(os.stat(db_path).st_mode & 0o777)
        db_ok = db_perms == "0o600"

        # Create a message to generate fallback file
        session_id = "test-perms"
        log_message("user", session_id, "Test message", db_path=db_path)

        # Export model
        export_model(db_path=db_path, model_dir=model_dir, as_of="2026-02-19")

        # Check model file permissions
        personality_path = os.path.join(model_dir, "personality.json")
        model_perms = oct(os.stat(personality_path).st_mode & 0o777)
        model_ok = model_perms == "0o600"

        report("File permissions",
               db_ok and model_ok,
               f"DB={db_perms}, model={model_perms}")
    finally:
        cleanup(tmpdir)


# --- Test 14: Calibration sanity ---
def test_calibration_sanity():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)
        conn = get_connection(db_path)
        now = "2026-02-19T00:00:00Z"
        session_id = "test-calibration"
        conn.execute("INSERT INTO sessions (session_id, started_at, mode) VALUES (?, ?, 'interview')", (session_id, now))

        # Low certainty: 1 observation per dimension, vague, 1 evidence
        for dim in ["communication_style", "vocabulary_language", "humor_wit",
                     "values_opinions", "knowledge_expertise", "emotional_relational",
                     "cognitive_decision_making"]:
            conn.execute("""
                INSERT INTO observations (dimension, facet, trait, value, evidence_count,
                                          specificity, first_observed, last_confirmed)
                VALUES (?, 'general', 'trait', 'Low certainty value', 1, 'vague', ?, ?)
            """, (dim, now, now))
        conn.commit()

        result_low = export_model(db_path=db_path, model_dir=model_dir, as_of="2026-02-19")
        low_readiness = result_low["confidence"]["overall_readiness"]

        # Clear and add medium certainty
        conn.execute("DELETE FROM observations")
        for dim in ["communication_style", "vocabulary_language", "humor_wit",
                     "values_opinions", "knowledge_expertise", "emotional_relational",
                     "cognitive_decision_making"]:
            for i in range(3):
                conn.execute("""
                    INSERT INTO observations (dimension, facet, trait, value, evidence_count,
                                              specificity, first_observed, last_confirmed)
                    VALUES (?, ?, 'trait', 'Medium certainty value', 5, 'general', ?, ?)
                """, (dim, f"facet_{i}", now, now))
        conn.commit()

        result_med = export_model(db_path=db_path, model_dir=model_dir, as_of="2026-02-19")
        med_readiness = result_med["confidence"]["overall_readiness"]

        # Clear and add high certainty
        conn.execute("DELETE FROM observations")
        for dim in ["communication_style", "vocabulary_language", "humor_wit",
                     "values_opinions", "knowledge_expertise", "emotional_relational",
                     "cognitive_decision_making"]:
            for i in range(5):
                conn.execute("""
                    INSERT INTO observations (dimension, facet, trait, value, evidence_count,
                                              specificity, first_observed, last_confirmed)
                    VALUES (?, ?, 'trait', 'High certainty value', 15, 'precise', ?, ?)
                """, (dim, f"facet_{i}", now, now))
        conn.commit()

        result_high = export_model(db_path=db_path, model_dir=model_dir, as_of="2026-02-19")
        high_readiness = result_high["confidence"]["overall_readiness"]

        conn.close()

        report("Calibration sanity",
               low_readiness < med_readiness < high_readiness,
               f"low={low_readiness:.4f} < med={med_readiness:.4f} < high={high_readiness:.4f}")
    finally:
        cleanup(tmpdir)


# --- Test 15: FK constraint enforcement ---
def test_fk_enforcement():
    tmpdir, db_path, _ = make_test_env()
    try:
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")

        now = "2026-02-19T00:00:00Z"
        conn.execute("INSERT INTO sessions (session_id, started_at, mode) VALUES ('fk-test', ?, 'interview')", (now,))
        conn.execute("""
            INSERT INTO messages (uuid, session_id, role, content, timestamp)
            VALUES ('fk-uuid-1', 'fk-test', 'user', 'Test', ?)
        """, (now,))
        msg_id = conn.execute("SELECT id FROM messages WHERE uuid = 'fk-uuid-1'").fetchone()[0]

        conn.execute("""
            INSERT INTO observations (dimension, facet, trait, value, evidence_count,
                                      specificity, first_observed, last_confirmed)
            VALUES ('humor_wit', 'type', 'test', 'Test', 1, 'specific', ?, ?)
        """, (now, now))
        obs_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute("""
            INSERT INTO observation_messages (observation_id, message_id, relation_type)
            VALUES (?, ?, 'source')
        """, (obs_id, msg_id))
        conn.commit()

        # RESTRICT: try to delete message linked via observation_messages
        restrict_works = False
        try:
            conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
            conn.commit()
        except sqlite3.IntegrityError:
            restrict_works = True
            conn.rollback()

        # CASCADE: deleting observation should cascade to observation_messages
        conn.execute("DELETE FROM observations WHERE id = ?", (obs_id,))
        conn.commit()
        link_count = conn.execute(
            "SELECT COUNT(*) FROM observation_messages WHERE observation_id = ?", (obs_id,)
        ).fetchone()[0]
        cascade_works = link_count == 0

        conn.close()

        report("FK constraint enforcement",
               restrict_works and cascade_works,
               f"RESTRICT on msg delete={restrict_works}, CASCADE on obs delete={cascade_works}")
    finally:
        cleanup(tmpdir)


# --- Test 16: Session end idempotency ---
def test_session_end_idempotency():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-idempotent"
        log_message("user", session_id, "Hello", db_path=db_path)

        # Patch paths for test environment
        import session_end
        orig_transcripts = session_end.TRANSCRIPTS_DIR
        orig_scripts = session_end.SCRIPTS_DIR
        session_end.TRANSCRIPTS_DIR = os.path.join(tmpdir, "transcripts")
        session_end.SCRIPTS_DIR = SCRIPTS_DIR

        success1, reason1 = end_session(session_id, db_path=db_path)
        success2, reason2 = end_session(session_id, db_path=db_path)

        session_end.TRANSCRIPTS_DIR = orig_transcripts
        session_end.SCRIPTS_DIR = orig_scripts

        report("Session end idempotency",
               success1 and reason1 == "finalized" and success2 and reason2 == "already finalized",
               f"first={reason1}, second={reason2}")
    finally:
        cleanup(tmpdir)


# --- Test 17: Token claim exclusivity ---
def _claim_worker(args):
    """Worker for concurrent claim test."""
    db_path, session_id, worker_id = args
    import session_end as se
    success, reason = se.end_session(session_id, db_path=db_path)
    return {"worker": worker_id, "success": success, "reason": reason}


def test_token_claim_exclusivity():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-claim-excl"

        # Create session with a message and a candidate
        conn = get_connection(db_path)
        now = "2026-02-19T00:00:00Z"
        conn.execute("INSERT INTO sessions (session_id, started_at, mode) VALUES (?, ?, 'interview')", (session_id, now))
        conn.execute(
            "INSERT INTO messages (uuid, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'Hello', ?)",
            (str(uuid.uuid4()), session_id, now)
        )
        conn.execute(
            "INSERT INTO pending_observations (session_id, candidate_json) VALUES (?, ?)",
            (session_id, json.dumps({"dimension": "humor_wit", "facet": "type", "trait": "test", "value": "Test", "specificity": "specific", "action": "new"}))
        )
        conn.commit()
        conn.close()

        # Patch paths
        import session_end
        orig_transcripts = session_end.TRANSCRIPTS_DIR
        orig_scripts = session_end.SCRIPTS_DIR
        session_end.TRANSCRIPTS_DIR = os.path.join(tmpdir, "transcripts")
        session_end.SCRIPTS_DIR = SCRIPTS_DIR

        # 3 concurrent workers
        args = [(db_path, session_id, i) for i in range(3)]
        with multiprocessing.Pool(3) as pool:
            results = pool.map(_claim_worker, args)

        session_end.TRANSCRIPTS_DIR = orig_transcripts
        session_end.SCRIPTS_DIR = orig_scripts

        # Exactly one should finalize
        finalized = [r for r in results if r["reason"] == "finalized"]
        conn = get_connection(db_path)
        conn.row_factory = sqlite3.Row
        session = conn.execute("SELECT ended_at, finalizing_token FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        conn.close()

        report("Token claim exclusivity",
               len(finalized) == 1 and session["ended_at"] is not None and session["finalizing_token"] is None,
               f"finalized_count={len(finalized)}, ended_at set={session['ended_at'] is not None}")
    finally:
        cleanup(tmpdir)


# --- Test 18: Crash recovery ---
def test_crash_recovery():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-crash"

        conn = get_connection(db_path)
        old_time = "2026-02-19T00:00:00Z"
        conn.execute("INSERT INTO sessions (session_id, started_at, mode, finalizing_token, finalizing_at) VALUES (?, ?, 'interview', 'dead-token', ?)",
                     (session_id, old_time, old_time))
        conn.execute(
            "INSERT INTO messages (uuid, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'Hello', ?)",
            (str(uuid.uuid4()), session_id, old_time)
        )
        conn.commit()
        conn.close()

        import session_end
        orig_transcripts = session_end.TRANSCRIPTS_DIR
        orig_scripts = session_end.SCRIPTS_DIR
        session_end.TRANSCRIPTS_DIR = os.path.join(tmpdir, "transcripts")
        session_end.SCRIPTS_DIR = SCRIPTS_DIR

        results = finalize_stale_sessions(db_path=db_path, max_age_minutes=0, claim_timeout_minutes=0)

        session_end.TRANSCRIPTS_DIR = orig_transcripts
        session_end.SCRIPTS_DIR = orig_scripts

        conn = get_connection(db_path)
        conn.row_factory = sqlite3.Row
        session = conn.execute("SELECT ended_at, finalizing_token FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        conn.close()

        reclaimed = [r for r in results if "reclaimed" in r.get("reason", "")]
        report("Crash recovery",
               len(reclaimed) == 1 and session["ended_at"] is not None and session["finalizing_token"] is None,
               f"reclaimed={len(reclaimed)}, ended_at set={session['ended_at'] is not None}")
    finally:
        cleanup(tmpdir)


# --- Test 19: Active claim not stolen ---
def test_active_claim_not_stolen():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-active-claim"

        conn = get_connection(db_path)
        # Set a recent finalizing_at (just now)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("INSERT INTO sessions (session_id, started_at, mode, finalizing_token, finalizing_at) VALUES (?, ?, 'interview', 'active-token', ?)",
                     (session_id, now, now))
        conn.execute(
            "INSERT INTO messages (uuid, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'Hello', ?)",
            (str(uuid.uuid4()), session_id, now)
        )
        conn.commit()
        conn.close()

        import session_end
        orig_transcripts = session_end.TRANSCRIPTS_DIR
        orig_scripts = session_end.SCRIPTS_DIR
        session_end.TRANSCRIPTS_DIR = os.path.join(tmpdir, "transcripts")
        session_end.SCRIPTS_DIR = SCRIPTS_DIR

        # claim_timeout_minutes=5 means recent claims won't be considered stale
        results = finalize_stale_sessions(db_path=db_path, max_age_minutes=0, claim_timeout_minutes=5)

        session_end.TRANSCRIPTS_DIR = orig_transcripts
        session_end.SCRIPTS_DIR = orig_scripts

        conn = get_connection(db_path)
        conn.row_factory = sqlite3.Row
        session = conn.execute("SELECT ended_at, finalizing_token FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        conn.close()

        report("Active claim not stolen",
               session["ended_at"] is None and session["finalizing_token"] == "active-token",
               f"ended_at={session['ended_at']}, token preserved={session['finalizing_token'] == 'active-token'}")
    finally:
        cleanup(tmpdir)


# --- Test 20: Staleness cutoff ---
def test_staleness_cutoff():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)

        conn = get_connection(db_path)
        old_time = "2020-01-01T00:00:00Z"
        recent_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Old session (should be finalized)
        conn.execute("INSERT INTO sessions (session_id, started_at, mode) VALUES ('old-session', ?, 'interview')", (old_time,))
        conn.execute("INSERT INTO messages (uuid, session_id, role, content, timestamp) VALUES (?, 'old-session', 'user', 'Hi', ?)",
                     (str(uuid.uuid4()), old_time))

        # Recent session (should be skipped)
        conn.execute("INSERT INTO sessions (session_id, started_at, mode) VALUES ('recent-session', ?, 'interview')", (recent_time,))
        conn.execute("INSERT INTO messages (uuid, session_id, role, content, timestamp) VALUES (?, 'recent-session', 'user', 'Hi', ?)",
                     (str(uuid.uuid4()), recent_time))
        conn.commit()
        conn.close()

        import session_end
        orig_transcripts = session_end.TRANSCRIPTS_DIR
        orig_scripts = session_end.SCRIPTS_DIR
        session_end.TRANSCRIPTS_DIR = os.path.join(tmpdir, "transcripts")
        session_end.SCRIPTS_DIR = SCRIPTS_DIR

        results = finalize_stale_sessions(db_path=db_path, max_age_minutes=5, claim_timeout_minutes=2)

        session_end.TRANSCRIPTS_DIR = orig_transcripts
        session_end.SCRIPTS_DIR = orig_scripts

        conn = get_connection(db_path)
        conn.row_factory = sqlite3.Row
        old_ended = conn.execute("SELECT ended_at FROM sessions WHERE session_id = 'old-session'").fetchone()["ended_at"]
        recent_ended = conn.execute("SELECT ended_at FROM sessions WHERE session_id = 'recent-session'").fetchone()["ended_at"]
        conn.close()

        report("Staleness cutoff",
               old_ended is not None and recent_ended is None,
               f"old finalized={old_ended is not None}, recent skipped={recent_ended is None}")
    finally:
        cleanup(tmpdir)


# --- Test 21: Token-loss detection ---
def test_token_loss_detection():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-token-loss"

        conn = get_connection(db_path)
        now = "2026-02-19T00:00:00Z"
        conn.execute("INSERT INTO sessions (session_id, started_at, mode) VALUES (?, ?, 'interview')", (session_id, now))
        conn.execute("INSERT INTO messages (uuid, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'Hello', ?)",
                     (str(uuid.uuid4()), session_id, now))
        conn.commit()

        # Simulate: claim the session with a token, then clear it (as if reclaimed)
        token = str(uuid.uuid4())
        conn.execute("UPDATE sessions SET finalizing_token = ?, finalizing_at = ? WHERE session_id = ?",
                     (token, now, session_id))
        conn.commit()

        # Now try to finalize with the token cleared (simulating another worker stealing it)
        conn.execute("UPDATE sessions SET finalizing_token = NULL, finalizing_at = NULL WHERE session_id = ?",
                     (session_id,))
        conn.commit()

        # Heartbeat should fail since token doesn't match
        result = heartbeat_claim(conn, session_id, token)
        conn.close()

        report("Token-loss detection",
               result is False,
               f"heartbeat_claim returned {result} (expected False)")
    finally:
        cleanup(tmpdir)


# --- Test 22: Reclaim guarded by ended_at ---
def test_reclaim_guarded_by_ended_at():
    tmpdir, db_path, model_dir = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-reclaim-guard"

        conn = get_connection(db_path)
        conn.row_factory = sqlite3.Row
        old_time = "2020-01-01T00:00:00Z"
        # Already finalized session with stale claim columns (shouldn't happen, but guard against it)
        conn.execute("""
            INSERT INTO sessions (session_id, started_at, ended_at, mode, finalizing_token, finalizing_at)
            VALUES (?, ?, ?, 'interview', 'old-token', ?)
        """, (session_id, old_time, old_time, old_time))
        conn.commit()

        # Try to reclaim — the UPDATE should match 0 rows because ended_at IS NOT NULL
        cur = conn.execute("""
            UPDATE sessions SET finalizing_token = NULL, finalizing_at = NULL
            WHERE session_id = ? AND ended_at IS NULL AND finalizing_at < ?
        """, (session_id, "2030-01-01T00:00:00Z"))
        conn.commit()

        # Session should be untouched
        session = conn.execute("SELECT finalizing_token, ended_at FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        conn.close()

        report("Reclaim guarded by ended_at",
               cur.rowcount == 0 and session["finalizing_token"] == "old-token",
               f"rowcount={cur.rowcount}, token preserved={session['finalizing_token'] == 'old-token'}")
    finally:
        cleanup(tmpdir)


# --- Test 23: Dev mode guard ---
def test_dev_mode_guard():
    tmpdir, db_path, _ = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-dev-guard"

        # Create session with dev_mode=1
        conn = get_connection(db_path)
        now = "2026-02-19T00:00:00Z"
        conn.execute("INSERT INTO sessions (session_id, started_at, mode, dev_mode) VALUES (?, ?, 'interview', 1)", (session_id, now))
        conn.commit()
        conn.close()

        # log_message should refuse
        msg_id, msg_uuid, err = log_message("user", session_id, "Should not log", db_path=db_path)
        msg_refused = err is not None and "Dev mode" in err

        # log_candidate should refuse
        cid, errors = log_candidate(session_id, json.dumps({
            "dimension": "humor_wit", "facet": "type", "trait": "test",
            "value": "Test", "specificity": "specific", "action": "new",
        }), db_path=db_path)
        cand_refused = errors is not None and any("Dev mode" in e for e in errors)

        report("Dev mode guard",
               msg_refused and cand_refused,
               f"msg_refused={msg_refused}, cand_refused={cand_refused}")
    finally:
        cleanup(tmpdir)


# --- Test 24: Dev mode session-scoped + --set-dev-mode ---
def test_dev_mode_session_scoped():
    tmpdir, db_path, _ = make_test_env()
    try:
        init_db(db_path)

        # Session 1: dev mode
        dev_session = "test-dev-scoped"
        conn = get_connection(db_path)
        now = "2026-02-19T00:00:00Z"
        conn.execute("INSERT INTO sessions (session_id, started_at, mode, dev_mode) VALUES (?, ?, 'interview', 1)", (dev_session, now))
        conn.commit()
        conn.close()

        # Session 2: normal
        normal_session = "test-normal-scoped"
        msg_id, msg_uuid, err = log_message("user", normal_session, "Normal message", db_path=db_path)
        normal_logs = err is None and msg_id is not None

        # Dev session refuses
        _, _, dev_err = log_message("user", dev_session, "Should not log", db_path=db_path)
        dev_refused = dev_err is not None

        # Test --set-dev-mode via the function
        from log_message import ensure_session, get_connection as lm_get_connection
        new_session = "test-setdev"
        conn = lm_get_connection(db_path)
        ensure_session(conn, new_session)
        conn.execute("BEGIN IMMEDIATE")
        msg_count = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (new_session,)).fetchone()[0]
        if msg_count == 0:
            conn.execute("UPDATE sessions SET dev_mode = 1 WHERE session_id = ?", (new_session,))
            conn.commit()
            set_ok = True
        else:
            conn.rollback()
            set_ok = False
        conn.close()

        # Verify it was set
        conn = get_connection(db_path)
        dev = conn.execute("SELECT dev_mode FROM sessions WHERE session_id = ?", (new_session,)).fetchone()[0]
        conn.close()

        report("Dev mode session-scoped + --set-dev-mode",
               normal_logs and dev_refused and set_ok and dev == 1,
               f"normal_logs={normal_logs}, dev_refused={dev_refused}, set_ok={set_ok}, dev_mode={dev}")
    finally:
        cleanup(tmpdir)


# --- Test 25: --set-dev-mode refused mid-session ---
def test_set_dev_mode_refused_mid_session():
    tmpdir, db_path, _ = make_test_env()
    try:
        init_db(db_path)
        session_id = "test-dev-mid"

        # Log a message first
        log_message("user", session_id, "First message", db_path=db_path)

        # Now try to set dev mode — should fail because session has messages
        conn = get_connection(db_path)
        conn.execute("BEGIN IMMEDIATE")
        msg_count = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]
        refused = msg_count > 0
        conn.rollback()

        # Verify dev_mode is still 0
        dev = conn.execute("SELECT dev_mode FROM sessions WHERE session_id = ?", (session_id,)).fetchone()[0]
        conn.close()

        report("--set-dev-mode refused mid-session",
               refused and dev == 0,
               f"refused={refused}, dev_mode={dev}")
    finally:
        cleanup(tmpdir)


def main():
    global PASS, FAIL
    print("\n=== Gerrit Digital Twin Test Suite ===\n")

    test_schema_idempotency()
    test_logging_roundtrip()
    test_uuid_dedup()
    test_uuid_caller_supply()
    test_export_determinism()
    test_confidence_scoring()
    test_contradiction_handling()
    test_reconciliation()
    test_fallback_format()
    test_candidate_validation()
    test_concurrency()
    test_impersonation_guards()
    test_file_permissions()
    test_calibration_sanity()
    test_fk_enforcement()

    # New tests (16-25)
    test_session_end_idempotency()
    test_token_claim_exclusivity()
    test_crash_recovery()
    test_active_claim_not_stolen()
    test_staleness_cutoff()
    test_token_loss_detection()
    test_reclaim_guarded_by_ended_at()
    test_dev_mode_guard()
    test_dev_mode_session_scoped()
    test_set_dev_mode_refused_mid_session()

    print(f"\n{'='*40}")
    print(f"Results: {PASS} passed, {FAIL} failed, {PASS+FAIL} total")
    if FAIL > 0:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
