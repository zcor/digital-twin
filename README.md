# Digital Twin

A system for building a conversational digital clone through iterative interviews. Everything learned persists in a local SQLite database and is exported as structured JSON model files. No external dependencies — just Python 3 and SQLite.

## How It Works

The system operates in three modes:

1. **Interview** — Claude learns about you through conversation, logging observations as structured candidates that get validated and stored at session end
2. **Calibration** — Test the model's accuracy by generating "what would you say?" responses and providing corrective feedback
3. **Impersonation** — Active simulation of you in conversation, with safety controls and opt-in requirements

Each session bootstraps from the database. Context resets every session — the DB is the only source of truth.

## The 7 Dimensions

Personality is modeled across seven dimensions, each with multiple facets:

| Dimension | What it captures |
|---|---|
| `communication_style` | Sentence structure, punctuation, hedging, formality, greetings |
| `vocabulary_language` | Characteristic words, jargon, metaphors, avoidances |
| `humor_wit` | Type, timing, references, what lands vs. falls flat |
| `values_opinions` | Core values, stances, hills to die on, flexibility |
| `knowledge_expertise` | Professional domains, hobbies, cultural knowledge |
| `emotional_relational` | Expression style, stress responses, empathy style |
| `cognitive_decision_making` | Reasoning style, risk tolerance, how they change their mind |

Each observation has a computed confidence score: `sigmoid(evidence) × recency × consistency × depth`.

## Architecture

```
scripts/
  init_db.py          # Idempotent schema setup (12 tables, WAL mode, FK constraints)
  log_message.py      # Per-message logging with UUID dedup and fallback transcript
  session_end.py      # Token+heartbeat claim protocol for safe concurrent finalization
  export_model.py     # Deterministic JSON export with confidence scoring
  test_suite.py       # 25 automated tests

model/                # Generated JSON (personality, confidence, gaps)
db/                   # SQLite database (gitignored)
transcripts/          # Fallback transcripts and session summaries (gitignored)

.claude/
  rules/
    logging.md        # Logging protocol, directives, candidate schema
    interviewing.md   # Interview strategy by readiness level
    impersonation.md  # Safety rules, hard blocks, disclosure requirements
  settings.json       # SessionStart hook for banner
```

No external dependencies. No pip installs. Just `python3` and the standard library.

## Quick Start

```bash
# Initialize the database
python3 scripts/init_db.py

# Generate empty model files
python3 scripts/export_model.py

# Run the test suite
python3 scripts/test_suite.py

# Start a Claude Code session — CLAUDE.md handles the rest
```

## Directives

Prefix messages with `#` directives to modify behavior:

| Directive | Description |
|---|---|
| `#meta` | Off-record — message not logged, no observations captured |
| `#dev` | Dev mode for entire session — must be first message, suppresses all logging |
| `#commands` | Show available directives |

## Session Lifecycle

Sessions use a token+heartbeat claim protocol for safe finalization:

- Each session gets a unique ID (`YYYY-MM-DD-NNN`)
- Messages are logged to both DB and a length-prefixed fallback transcript
- On session end, a UUID token claims the session atomically
- Heartbeats prove liveness during processing — stale claims get reclaimed
- Orphaned sessions from crashed windows are cleaned up on next startup

## Safety Model

- **Impersonation requires explicit opt-in** each session (resets automatically)
- **Disclosure mode** prepends `[AI SIMULATION — not the real Gerrit]` to impersonation responses
- **Hard blocks**: no financial, legal, or reputational harm; no unsupervised deception; no credential use
- **Contradictions are data** — the system models nuance rather than resolving conflicts
- **Candidates, not direct writes** — observations go through a validation pipeline before becoming permanent

## License

MIT
