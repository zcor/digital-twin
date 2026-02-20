# Logging Protocol

## Real-Time Message Logging

Every message — user and assistant — must be logged immediately via `log_message.py`. Use `--content-stdin` to avoid shell quoting issues with special characters.

### Flow per message:
1. Receive/generate message
2. Pipe content to `log_message.py` via stdin
3. Capture returned `{"message_id": N, "uuid": "..."}` for reference
4. If logging fails, note the error but continue the conversation — the fallback transcript provides recovery

## Observation Candidate Schema (V1)

When you observe something about Gerrit, submit it as a structured JSON candidate:

```json
{
  "$schema_version": 1,
  "dimension": "string, required",
  "facet": "string, required, max 50 chars",
  "trait": "string, required, max 50 chars",
  "value": "string, required, max 500 chars",
  "specificity": "string, required",
  "action": "string, required",
  "observation_id": "int, required for confirm/contradict",
  "source_message_ids": "array of ints, optional",
  "context": "string, optional, max 200 chars"
}
```

### Dimension enums (exactly 7):
- `communication_style` — sentence structure, punctuation, hedging, formality, greetings
- `vocabulary_language` — characteristic words, jargon, metaphors, avoidances
- `humor_wit` — type, timing, references, what lands vs. falls flat
- `values_opinions` — core values, stances, hills to die on, flexibility
- `knowledge_expertise` — professional domains, hobbies, cultural knowledge
- `emotional_relational` — expression style, stress responses, empathy style
- `cognitive_decision_making` — reasoning style, risk tolerance, how they change their mind

### Specificity levels:
- `vague` — general impression, no specific evidence ("seems friendly")
- `general` — pattern observed but not precisely characterized
- `specific` — clear pattern with at least one concrete example
- `precise` — well-characterized with multiple examples and context

### Action types:
- `new` — first observation of this trait
- `confirm` — reinforces existing observation (requires `observation_id`)
- `contradict` — conflicts with existing observation (requires `observation_id`, should include `context`)
- `uncertain` — not enough data to commit, adds to gaps table

### When to log observations:
- After Gerrit makes a distinctive statement or uses characteristic language
- When you notice a pattern across multiple messages in the session
- When something contradicts an existing observation (both are valuable!)
- At natural conversation breaks, review recent messages for missed observations

### Quality guidelines:
- Prefer `specific` or `precise` over `vague` when possible
- Include `source_message_ids` when you have them
- For contradictions, always include `context` explaining the difference
- One observation per candidate — don't combine multiple traits
- Value should be descriptive, not just a label. "Uses dry sarcasm with deadpan delivery, especially when discussing JavaScript" > "is sarcastic"

## Fallback Transcript Format

Length-prefixed records in `transcripts/fallback/{session_id}.txt`:

```
GERRIT_RECORD <uuid> <role> <utc_timestamp> <content_byte_length>
<content bytes>
```

This is written automatically by `log_message.py`. The byte-length prefix ensures deterministic parsing regardless of content.
