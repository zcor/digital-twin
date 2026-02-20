#!/usr/bin/env python3
"""Regenerate model JSON files from DB.

Deterministic: sort_keys=True, confidence rounded to 4 decimal places, all timestamps UTC.
Accepts --as-of DATE for testing recency calculations against a fixed date.

Outputs:
    model/personality.json  — active observations by dimension/facet (confidence > 0.2)
    model/confidence.json   — computed confidence scores, readiness, blocking reasons
    model/gaps.json         — prioritized knowledge gaps
"""

import argparse
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "gerrit.db")
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "model")

VALID_DIMENSIONS = [
    "communication_style", "vocabulary_language", "humor_wit",
    "values_opinions", "knowledge_expertise", "emotional_relational",
    "cognitive_decision_making"
]

DIMENSION_FACET_COUNTS = {
    "communication_style": 8,
    "vocabulary_language": 6,
    "humor_wit": 4,
    "values_opinions": 6,
    "knowledge_expertise": 5,
    "emotional_relational": 6,
    "cognitive_decision_making": 5,
}

DEPTH_MAP = {"vague": 0.4, "general": 0.6, "specific": 0.8, "precise": 1.0}

# Readiness thresholds
CALIBRATION_READINESS = 0.40
CALIBRATION_MIN_DIM = 0.30
CALIBRATION_MAX_CONTRADICTION = 0.20

IMPERSONATION_READINESS = 0.65
IMPERSONATION_MIN_DIM = 0.50
IMPERSONATION_MAX_CONTRADICTION = 0.10


def get_connection(db_path=None):
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def count_contradiction_links(conn, obs_id):
    """Count active contradiction links for an observation."""
    row = conn.execute("""
        SELECT COUNT(*) FROM observation_links
        WHERE (from_observation_id = ? OR to_observation_id = ?)
        AND link_type = 'contradicts'
    """, (obs_id, obs_id)).fetchone()
    return row[0]


def observation_confidence(obs, contradiction_count, as_of=None):
    """Compute confidence for a single observation using the exact formula."""
    today = as_of or datetime.now(timezone.utc).date()

    # Base: sigmoid on evidence count
    b = 1.0 / (1.0 + math.exp(-0.5 * (obs["evidence_count"] - 5)))

    # Recency: linear decay from last confirmation
    last_confirmed = datetime.strptime(obs["last_confirmed"][:10], "%Y-%m-%d").date()
    days = (today - last_confirmed).days
    r = max(0.5, 1.0 - (days / 365) * 0.5)

    # Consistency: penalty per active contradiction link
    c = max(0.2, 1.0 - 0.2 * contradiction_count)

    # Depth: from specificity enum
    d = DEPTH_MAP.get(obs["specificity"], 0.6)

    return round(b * r * c * d, 4)


def facet_confidence(observations_with_conf):
    """Weighted average confidence for a facet's observations."""
    if not observations_with_conf:
        return 0.0
    weights = [o["evidence_count"] for o in observations_with_conf]
    scores = [o["computed_confidence"] for o in observations_with_conf]
    return round(sum(s * w for s, w in zip(scores, weights)) / sum(weights), 4)


def dimension_confidence(dimension, facet_scores):
    """Dimension confidence including coverage penalty."""
    expected = DIMENSION_FACET_COUNTS.get(dimension, 5)
    observed = sum(1 for s in facet_scores.values() if s > 0)
    coverage = observed / expected
    mean_score = sum(facet_scores.values()) / expected  # zeros for missing facets
    return round(mean_score * coverage, 4)


def readiness_score(dim_scores, exemplar_ratio, contradiction_rate, vocab_score):
    """Overall impersonation readiness."""
    if not dim_scores:
        return 0.0
    min_dim = min(dim_scores.values())
    avg_dim = sum(dim_scores.values()) / len(dim_scores)
    return round(
        0.25 * min_dim
        + 0.25 * avg_dim
        + 0.20 * exemplar_ratio
        + 0.15 * (1.0 - contradiction_rate)
        + 0.15 * vocab_score,
        4
    )


def export_model(db_path=None, model_dir=None, as_of=None):
    """Main export function. Returns dict of generated data for testing."""
    db_path = db_path or DB_PATH
    model_dir = model_dir or MODEL_DIR
    os.makedirs(model_dir, exist_ok=True)

    if not os.path.exists(db_path):
        return generate_empty_model(model_dir, as_of)

    conn = get_connection(db_path)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if as_of:
        if isinstance(as_of, str):
            as_of_date = datetime.strptime(as_of, "%Y-%m-%d").date()
        else:
            as_of_date = as_of
    else:
        as_of_date = None

    # --- Build personality.json ---
    observations = conn.execute("""
        SELECT id, dimension, facet, trait, value, confidence, evidence_count,
               specificity, first_observed, last_confirmed, is_active
        FROM observations WHERE is_active = 1
    """).fetchall()

    # Compute actual confidence for each observation
    obs_data = []
    for obs in observations:
        cc = count_contradiction_links(conn, obs["id"])
        conf = observation_confidence(obs, cc, as_of=as_of_date)
        obs_dict = dict(obs)
        obs_dict["computed_confidence"] = conf
        obs_dict["contradiction_count"] = cc
        obs_data.append(obs_dict)

    # Update stored confidence values
    for od in obs_data:
        conn.execute("UPDATE observations SET confidence = ? WHERE id = ?", (od["computed_confidence"], od["id"]))
    conn.commit()

    # Group by dimension/facet
    personality = {}
    dim_facet_obs = defaultdict(lambda: defaultdict(list))
    for od in obs_data:
        dim_facet_obs[od["dimension"]][od["facet"]].append(od)

    for dim in VALID_DIMENSIONS:
        personality[dim] = {}
        if dim in dim_facet_obs:
            for facet, facet_obs in sorted(dim_facet_obs[dim].items()):
                visible = [o for o in facet_obs if o["computed_confidence"] > 0.2]
                if visible:
                    personality[dim][facet] = [
                        {
                            "trait": o["trait"],
                            "value": o["value"],
                            "confidence": o["computed_confidence"],
                            "evidence_count": o["evidence_count"],
                            "specificity": o["specificity"],
                        }
                        for o in sorted(visible, key=lambda x: -x["computed_confidence"])
                    ]
        if not personality[dim]:
            personality[dim] = {"_status": "unknown", "_rationale": f"No observations yet for {dim.replace('_', ' ')}"}

    personality_out = {
        "generated_at": now_utc,
        "schema_version": 1,
        "dimensions": personality,
    }

    # --- Build confidence.json ---
    facet_scores_by_dim = {}
    for dim in VALID_DIMENSIONS:
        facet_scores = {}
        if dim in dim_facet_obs:
            for facet, facet_obs in dim_facet_obs[dim].items():
                facet_scores[facet] = facet_confidence(facet_obs)
        facet_scores_by_dim[dim] = facet_scores

    dim_scores = {}
    per_dimension = {}
    for dim in VALID_DIMENSIONS:
        fs = facet_scores_by_dim[dim]
        ds = dimension_confidence(dim, fs)
        dim_scores[dim] = ds

        dim_obs = [o for o in obs_data if o["dimension"] == dim]
        total_contradictions = sum(o["contradiction_count"] for o in dim_obs)

        per_dimension[dim] = {
            "confidence": ds,
            "coverage": round(sum(1 for s in fs.values() if s > 0) / DIMENSION_FACET_COUNTS.get(dim, 5), 4),
            "min_facet_confidence": round(min(fs.values(), default=0.0), 4),
            "contradiction_count": total_contradictions,
            "facets": {k: round(v, 4) for k, v in sorted(fs.items())},
        }

    # Exemplar ratio
    total_obs = len([o for o in obs_data if o["computed_confidence"] > 0.2])
    obs_with_exemplars = conn.execute("""
        SELECT COUNT(DISTINCT observation_id) FROM exemplars
        WHERE observation_id IN (SELECT id FROM observations WHERE is_active = 1)
    """).fetchone()[0]
    exemplar_ratio = round(obs_with_exemplars / max(total_obs, 1), 4)

    # Contradiction rate
    total_links = conn.execute("SELECT COUNT(*) FROM observation_links WHERE link_type = 'contradicts'").fetchone()[0]
    total_active_obs = len(obs_data)
    contradiction_rate = round(total_links / max(total_active_obs, 1), 4)

    # Vocabulary score
    vocab_count = conn.execute("SELECT COUNT(*) FROM vocabulary").fetchone()[0]
    vocab_score = round(min(vocab_count / 50.0, 1.0), 4)  # 50 terms = max score

    overall = readiness_score(dim_scores, exemplar_ratio, contradiction_rate, vocab_score)

    # Blocking reasons
    blocking_reasons = []
    for dim in VALID_DIMENSIONS:
        ds = dim_scores[dim]
        if ds < CALIBRATION_MIN_DIM:
            blocking_reasons.append(f"{dim} dimension at {ds:.2f}, needs >= {CALIBRATION_MIN_DIM:.2f} for calibration")

    if contradiction_rate >= CALIBRATION_MAX_CONTRADICTION:
        blocking_reasons.append(f"Contradiction rate {contradiction_rate:.2f} >= {CALIBRATION_MAX_CONTRADICTION:.2f}")

    sorted_dims = sorted(dim_scores.items(), key=lambda x: x[1])
    weakest = [d[0] for d in sorted_dims[:3]]
    strongest = [d[0] for d in sorted_dims[-3:]]

    confidence_out = {
        "generated_at": now_utc,
        "schema_version": 1,
        "overall_readiness": overall,
        "per_dimension": per_dimension,
        "weakest_dimensions": weakest,
        "strongest_dimensions": strongest,
        "exemplar_ratio": exemplar_ratio,
        "contradiction_rate": contradiction_rate,
        "vocabulary_score": vocab_score,
        "ready_for_calibration": overall >= CALIBRATION_READINESS and all(d >= CALIBRATION_MIN_DIM for d in dim_scores.values()) and contradiction_rate < CALIBRATION_MAX_CONTRADICTION,
        "ready_for_impersonation": overall >= IMPERSONATION_READINESS and all(d >= IMPERSONATION_MIN_DIM for d in dim_scores.values()) and contradiction_rate < IMPERSONATION_MAX_CONTRADICTION,
        "blocking_reasons": blocking_reasons,
    }

    # --- Build gaps.json ---
    gaps_rows = conn.execute("""
        SELECT dimension, facet, description, priority, suggested_approaches, status
        FROM gaps WHERE status != 'resolved'
        ORDER BY priority DESC
    """).fetchall()

    gaps_out = {
        "generated_at": now_utc,
        "schema_version": 1,
        "gaps": [
            {
                "dimension": g["dimension"],
                "facet": g["facet"],
                "description": g["description"],
                "priority": round(g["priority"], 4),
                "suggested_approaches": json.loads(g["suggested_approaches"]) if g["suggested_approaches"] else None,
                "state": "partially_known" if g["status"] == "partially_addressed" else "unknown",
                "confidence_rationale": f"Gap in {g['dimension']}/{g['facet'] or 'general'}: {g['description']}",
            }
            for g in gaps_rows
        ],
    }

    conn.close()

    # Write files
    write_json(os.path.join(model_dir, "personality.json"), personality_out)
    write_json(os.path.join(model_dir, "confidence.json"), confidence_out)
    write_json(os.path.join(model_dir, "gaps.json"), gaps_out)

    return {"personality": personality_out, "confidence": confidence_out, "gaps": gaps_out}


def generate_empty_model(model_dir, as_of=None):
    """Generate model files when DB doesn't exist or is empty."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    personality = {}
    for dim in VALID_DIMENSIONS:
        personality[dim] = {
            "_status": "unknown",
            "_rationale": f"No observations yet for {dim.replace('_', ' ')}. Needs interview data.",
        }

    personality_out = {"generated_at": now_utc, "schema_version": 1, "dimensions": personality}

    dim_scores = {dim: 0.0 for dim in VALID_DIMENSIONS}
    per_dimension = {}
    for dim in VALID_DIMENSIONS:
        per_dimension[dim] = {
            "confidence": 0.0,
            "coverage": 0.0,
            "min_facet_confidence": 0.0,
            "contradiction_count": 0,
            "facets": {},
        }

    confidence_out = {
        "generated_at": now_utc,
        "schema_version": 1,
        "overall_readiness": 0.0,
        "per_dimension": per_dimension,
        "weakest_dimensions": VALID_DIMENSIONS[:3],
        "strongest_dimensions": VALID_DIMENSIONS[-3:],
        "exemplar_ratio": 0.0,
        "contradiction_rate": 0.0,
        "vocabulary_score": 0.0,
        "ready_for_calibration": False,
        "ready_for_impersonation": False,
        "blocking_reasons": [f"{dim} dimension at 0.00, needs >= 0.30 for calibration" for dim in VALID_DIMENSIONS],
    }

    gaps_out = {
        "generated_at": now_utc,
        "schema_version": 1,
        "gaps": [
            {
                "dimension": dim,
                "facet": None,
                "description": f"No data collected for {dim.replace('_', ' ')}",
                "priority": 0.8,
                "suggested_approaches": ["Direct interview questions about this dimension"],
                "state": "unknown",
                "confidence_rationale": f"Zero observations in {dim.replace('_', ' ')} — highest priority for data collection",
            }
            for dim in VALID_DIMENSIONS
        ],
    }

    write_json(os.path.join(model_dir, "personality.json"), personality_out)
    write_json(os.path.join(model_dir, "confidence.json"), confidence_out)
    write_json(os.path.join(model_dir, "gaps.json"), gaps_out)

    return {"personality": personality_out, "confidence": confidence_out, "gaps": gaps_out}


def write_json(filepath, data):
    """Write JSON with deterministic formatting and 0600 permissions."""
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    try:
        os.chmod(filepath, 0o600)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description="Export model JSON from DB")
    parser.add_argument("--db", help="Override database path")
    parser.add_argument("--model-dir", help="Override model output directory")
    parser.add_argument("--as-of", help="Fixed date for recency calculations (YYYY-MM-DD)")

    args = parser.parse_args()

    as_of = None
    if args.as_of:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()

    result = export_model(db_path=args.db, model_dir=args.model_dir, as_of=as_of)
    print(f"Model exported: {len(result['personality']['dimensions'])} dimensions, "
          f"readiness={result['confidence']['overall_readiness']:.4f}")


if __name__ == "__main__":
    main()
