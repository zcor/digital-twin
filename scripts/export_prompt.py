#!/usr/bin/env python3
"""Generate a condensed natural-language system prompt from model files.

Reads personality.json and confidence.json, filters to high-confidence observations,
converts to condensed prose organized by dimension, and bakes in impersonation rules.

Target: ~3,000–5,000 tokens (~12K–20K chars). Aggressively condenses observation values
to trait-name summaries rather than full descriptions.

Outputs: model/system_prompt.txt
"""

import json
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "gerrit.db")
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "model")

MIN_CONFIDENCE = 0.10
MAX_OBS_PER_FACET = 5
MAX_VALUE_LEN = 100
MAX_EXEMPLARS = 12
MAX_VOCAB_PER_CATEGORY = 10

DIMENSION_LABELS = {
    "communication_style": "Communication Style",
    "vocabulary_language": "Vocabulary & Language",
    "humor_wit": "Humor & Wit",
    "values_opinions": "Values & Opinions",
    "knowledge_expertise": "Knowledge & Expertise",
    "emotional_relational": "Emotional & Relational",
    "cognitive_decision_making": "Cognitive & Decision-Making",
}

FACET_LABELS = {
    "tone": "Tone",
    "cadence": "Cadence",
    "greetings": "Greetings",
    "hedging": "Hedging",
    "punctuation": "Punctuation",
    "catchphrases": "Catchphrases",
    "jargon": "Jargon",
    "avoidances": "Avoidances",
    "type": "Type",
    "timing": "Timing",
    "core_values": "Core Values",
    "lifestyle": "Lifestyle",
    "technology_preferences": "Technology",
    "professional": "Professional",
    "hobbies": "Hobbies",
    "cultural": "Cultural",
    "expression_style": "Expression Style",
    "relationship_style": "Relationships",
    "stress_response": "Stress Response",
    "reasoning_style": "Reasoning",
    "work_style": "Work Style",
    "risk_tolerance": "Risk Tolerance",
    "motivation_style": "Motivation",
    "feedback_style": "Feedback",
    "self_awareness": "Self-Awareness",
    "worldview": "Worldview",
    "tool_usage": "Tool Usage",
}


def truncate(text, max_len):
    """Truncate text to max_len, breaking at word boundary."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    # Break at last space
    last_space = truncated.rfind(" ")
    if last_space > max_len * 0.6:
        truncated = truncated[:last_space]
    return truncated.rstrip(".,;: ") + "..."


def load_vocabulary(db_path=None):
    """Load vocabulary terms from DB, grouped by category."""
    db_path = db_path or DB_PATH
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT term, category, frequency FROM vocabulary ORDER BY session_count DESC"
    ).fetchall()
    conn.close()

    by_category = {}
    for r in rows:
        cat = r["category"]
        by_category.setdefault(cat, []).append({
            "term": r["term"],
            "frequency": r["frequency"],
        })
    return by_category


def load_exemplars(db_path=None):
    """Load top exemplar quotes from DB."""
    db_path = db_path or DB_PATH
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT e.quote, o.dimension, o.trait
        FROM exemplars e
        JOIN observations o ON e.observation_id = o.id
        WHERE o.is_active = 1 AND o.confidence >= ?
        ORDER BY o.confidence DESC, e.id DESC
        LIMIT ?
    """, (MIN_CONFIDENCE, MAX_EXEMPLARS)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_dimension_prose(facets_data):
    """Convert a dimension's observations into condensed bullet points.

    Takes top N observations per facet by confidence, truncates values.
    """
    lines = []
    for facet_key, obs_list in sorted(facets_data.items()):
        if not isinstance(obs_list, list):
            continue
        high_conf = [o for o in obs_list if o.get("confidence", 0) >= MIN_CONFIDENCE]
        if not high_conf:
            continue

        facet_label = FACET_LABELS.get(facet_key, facet_key.replace("_", " ").title())
        top = sorted(high_conf, key=lambda x: -x["confidence"])[:MAX_OBS_PER_FACET]

        trait_summaries = []
        for o in top:
            # Use trait name as label, truncated value as description
            trait_name = o["trait"].replace("_", " ")
            value = truncate(o["value"], MAX_VALUE_LEN)
            trait_summaries.append(f"{trait_name}: {value}")

        lines.append(f"- **{facet_label}**: {'; '.join(trait_summaries)}")

    return "\n".join(lines)


def build_vocabulary_section(vocab_by_category):
    """Build compact vocabulary reference."""
    if not vocab_by_category:
        return ""

    lines = []
    for cat, terms in sorted(vocab_by_category.items()):
        cat_label = cat.replace("_", " ").title()
        term_list = ", ".join(f'"{t["term"]}"' for t in terms[:MAX_VOCAB_PER_CATEGORY])
        lines.append(f"- **{cat_label}**: {term_list}")

    return "## Vocabulary\n" + "\n".join(lines)


def build_exemplars_section(exemplars):
    """Build compact exemplar quotes section."""
    if not exemplars:
        return ""

    lines = []
    for ex in exemplars:
        quote = ex["quote"].strip()
        if len(quote) > 150:
            quote = truncate(quote, 150)
        lines.append(f'- "{quote}"')

    return "## Exemplar Quotes\nStyle reference — adapt, don't copy.\n" + "\n".join(lines)


def export_prompt(db_path=None, model_dir=None):
    """Generate system_prompt.txt from model files and DB."""
    model_dir = model_dir or MODEL_DIR
    db_path = db_path or DB_PATH

    personality_path = os.path.join(model_dir, "personality.json")
    confidence_path = os.path.join(model_dir, "confidence.json")

    if not os.path.exists(personality_path):
        print("No personality.json found — skipping prompt export")
        return None

    with open(personality_path) as f:
        personality = json.load(f)
    with open(confidence_path) as f:
        confidence = json.load(f)

    sections = [
        "You are Gerrit Hall. Respond as him — his voice, opinions, patterns. You are a simulation, not the real person.",
        "",
        "## Rules",
        "Default: 1–3 sentences. Terse. Minimum viable answers. Only go longer for: data/stats he cares about, stories he's into, technical explanations in his domain.",
        "Greeting/small talk: 1 sentence + quip. Opinion: 1–2 sentences, blunt. \"Tell me about yourself\": 2–3 max, deflect. Technical: short paragraph. Emotional: short, understated, may deflect with humor.",
        "NEVER: bullet lists, multiple paragraphs per question, volunteering unrequested info, tidy summaries, emoji, being diplomatic when he'd be blunt.",
        "Unknown topics: one deflection (\"haven't really thought about that\"), then redirect to projects/AI/fitness/opinions.",
        "First message only: brief note that you're an AI simulation. After that, no disclaimers.",
        "",
        "## Hard Blocks",
        "Never: authorize transactions, speak in legal contexts, damage his reputation, pretend to be real Gerrit to deceive, reveal credentials. Break character to refuse.",
    ]

    # Personality dimensions
    sections.append("\n## Personality")

    dim_order = [
        "communication_style", "vocabulary_language", "humor_wit",
        "values_opinions", "knowledge_expertise", "emotional_relational",
        "cognitive_decision_making",
    ]

    for dim_key in dim_order:
        dim_label = DIMENSION_LABELS.get(dim_key, dim_key)
        facets = personality["dimensions"].get(dim_key, {})
        if isinstance(facets, dict) and "_status" in facets:
            continue
        prose = build_dimension_prose(facets)
        if prose:
            dim_conf = confidence.get("per_dimension", {}).get(dim_key, {}).get("confidence", 0)
            conf_note = " (lower confidence)" if dim_conf < 0.07 else ""
            sections.extend([f"\n### {dim_label}{conf_note}", prose])

    # Vocabulary
    vocab = load_vocabulary(db_path)
    vocab_section = build_vocabulary_section(vocab)
    if vocab_section:
        sections.extend(["", vocab_section])

    # Exemplars
    exemplars = load_exemplars(db_path)
    exemplars_section = build_exemplars_section(exemplars)
    if exemplars_section:
        sections.extend(["", exemplars_section])

    prompt_text = "\n".join(sections) + "\n"

    output_path = os.path.join(model_dir, "system_prompt.txt")
    with open(output_path, "w") as f:
        f.write(prompt_text)
    try:
        os.chmod(output_path, 0o600)
    except OSError:
        pass

    est_tokens = len(prompt_text) // 4
    print(f"System prompt exported: {len(prompt_text)} chars, ~{est_tokens} tokens")
    return prompt_text


def main():
    export_prompt()


if __name__ == "__main__":
    main()
