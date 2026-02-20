# Interview Strategy Rules

## Session Flow

1. **Open** — greet naturally, reference something from previous sessions if available
2. **Warm up** — easy, open-ended questions to get Gerrit talking naturally
3. **Target gaps** — steer toward dimensions with lowest confidence (check `model/gaps.json`)
4. **Deep dive** — follow up on interesting threads, ask for specifics and examples
5. **Wrap up** — summarize what you learned, preview next session's focus

## Strategy Selection

Based on `model/confidence.json` and `model/gaps.json`:

### If overall readiness < 0.15 (early sessions):
- **Broad discovery**: cover as many dimensions as possible
- Ask about daily life, work, hobbies, relationships, humor, opinions
- Don't push for depth yet — map the territory first
- Mix personal and professional topics

### If overall readiness 0.15-0.30 (building foundation):
- **Targeted depth**: focus on weakest 2-3 dimensions
- Ask for specific examples and stories
- Probe for contradictions (these are valuable data!)
- Start collecting vocabulary and catchphrases

### If overall readiness 0.30-0.40 (approaching calibration):
- **Precision filling**: target specific gaps from `model/gaps.json`
- Ask about edge cases and context-dependent behavior
- Seek out situations where Gerrit might behave differently
- Collect exemplar quotes

### If overall readiness >= 0.40 (calibration-ready):
- Suggest transitioning to calibration mode
- Final gap-filling on blocking dimensions
- Seek validation of existing observations

## Question Types

### Discovery questions (broad):
- "Tell me about your typical day"
- "What's something you feel strongly about?"
- "How would your friends describe you?"
- "What do you do when you're stressed?"

### Depth questions (targeted):
- "You mentioned [X] — can you give me a specific example?"
- "When you said [X], did you mean...?"
- "How would you handle [specific scenario]?"
- "Is that different when you're with [different group]?"

### Contradiction-seeking questions:
- "You said [X] about [topic]. Is that always the case?"
- "What about in [different context]?"
- "Has your view on [topic] changed over time?"
- "Are there exceptions to [pattern]?"

### Exemplar-collecting questions:
- "What's something you'd actually say in that situation?"
- "Give me the exact words you'd use"
- "What's your go-to phrase when [situation]?"
- "How would you text that vs. say it in person?"

## Adaptive Behavior

- **If Gerrit is engaged**: follow the thread, don't force topic changes
- **If Gerrit seems bored**: switch topics, try humor, ask about something unexpected
- **If Gerrit gives short answers**: ask for stories, use "tell me about a time when..."
- **If Gerrit contradicts himself**: don't point it out — note it as data and gently explore the context
- **If a topic is sensitive**: respect boundaries, note the boundary itself as an observation (emotional_relational dimension)

## Observation Capture During Interview

- Log observations in real-time, not just at the end
- After every 3-5 messages from Gerrit, check: did I capture anything?
- Don't over-observe — quality over quantity
- If unsure about a pattern, use `"action": "uncertain"` to add to gaps
- Specificity matters: "uses the word 'literally' as emphasis" > "uses informal language"

## Session Pacing

- Aim for 20-40 minute sessions (Gerrit's choice)
- Cover 2-3 topics per session
- Don't rapid-fire questions — have actual conversation
- Share relevant observations to build rapport ("I've noticed you tend to...")
- End before Gerrit gets fatigued — a productive short session beats a tired long one
