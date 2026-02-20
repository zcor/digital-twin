# Confidence & Readiness Scoring Reference

All timestamps UTC. All formulas accept injectable `as_of` date for testing.

## Per-Observation Confidence

```python
def observation_confidence(obs, as_of=None):
    today = as_of or datetime.utcnow().date()

    # Base: sigmoid on evidence count
    b = 1.0 / (1.0 + math.exp(-0.5 * (obs.evidence_count - 5)))

    # Recency: linear decay from last confirmation
    days = (today - obs.last_confirmed).days
    r = max(0.5, 1.0 - (days / 365) * 0.5)

    # Consistency: penalty per active contradiction link
    contradiction_count = count_links(obs.id, 'contradicts')
    c = max(0.2, 1.0 - 0.2 * contradiction_count)

    # Depth: from specificity enum
    DEPTH = {"vague": 0.4, "general": 0.6, "specific": 0.8, "precise": 1.0}
    d = DEPTH[obs.specificity]

    return round(b * r * c * d, 4)
```

### Components

| Component | Range | Description |
|-----------|-------|-------------|
| Base (b) | 0-1 | Sigmoid on evidence count, centered at 5 |
| Recency (r) | 0.5-1.0 | Linear decay over 365 days, floor at 0.5 |
| Consistency (c) | 0.2-1.0 | -0.2 per contradiction link, floor at 0.2 |
| Depth (d) | 0.4-1.0 | From specificity: vague=0.4, general=0.6, specific=0.8, precise=1.0 |

## Per-Facet Confidence

Weighted average where weight = evidence_count:

```python
def facet_confidence(observations):
    if not observations:
        return 0.0
    weights = [obs.evidence_count for obs in observations]
    scores = [observation_confidence(obs) for obs in observations]
    return round(sum(s * w for s, w in zip(scores, weights)) / sum(weights), 4)
```

## Per-Dimension Confidence

Includes coverage penalty for missing facets:

```python
DIMENSION_FACET_COUNTS = {
    "communication_style": 8, "vocabulary_language": 6,
    "humor_wit": 4, "values_opinions": 6,
    "knowledge_expertise": 5, "emotional_relational": 6,
    "cognitive_decision_making": 5
}

def dimension_confidence(dimension, facet_scores):
    expected = DIMENSION_FACET_COUNTS[dimension]
    observed = sum(1 for s in facet_scores.values() if s > 0)
    coverage = observed / expected
    mean_score = sum(facet_scores.values()) / expected  # zeros for missing
    return round(mean_score * coverage, 4)
```

## Impersonation Readiness

```python
def readiness(dim_scores, exemplar_ratio, contradiction_rate, vocab_score):
    return round(
        0.25 * min(dim_scores.values())
      + 0.25 * (sum(dim_scores.values()) / len(dim_scores))
      + 0.20 * exemplar_ratio
      + 0.15 * (1.0 - contradiction_rate)
      + 0.15 * vocab_score
    , 4)
```

| Weight | Component | Description |
|--------|-----------|-------------|
| 0.25 | Min dimension | Weakest link — all dimensions must be adequate |
| 0.25 | Avg dimension | Overall model richness |
| 0.20 | Exemplar ratio | % of observations with direct quotes |
| 0.15 | 1 - contradiction rate | Model consistency |
| 0.15 | Vocabulary score | min(vocab_count/50, 1.0) |

## Mode Transition Thresholds

| Transition | Readiness | Per-dim min | Contradiction rate |
|---|---|---|---|
| INTERVIEW -> CALIBRATION | >= 0.40 | all >= 0.30 | < 0.20 |
| CALIBRATION -> IMPERSONATION | >= 0.65 | all >= 0.50 | < 0.10 |

`blocking_reasons` in confidence.json lists each failing condition.
