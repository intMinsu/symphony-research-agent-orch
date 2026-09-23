# EXP-001: Add one reproducible evaluation ablation

Revision: 1
Spec SHA-256: `8bb5ca2f64a5ba14b887ba32ea1446ee906c4830e379a4a329ebcde183530a8c`
Kind: experiment

## Objective

Implement a bounded evaluation ablation and report results without changing the frozen baseline.

## Scope

Include: `src/evaluation/*`, `tests/evaluation/*`, `reports/EXP-001.md`
Exclude: `data/*`, `baselines/*`

## Acceptance and evidence

- [ ] **AC-1**: A fixed-seed evaluation command is covered by tests.
  Evidence: The registered unit check passes and the report records the exact command and seed.
- [ ] **AC-2**: The report distinguishes observed, negative, and inconclusive outcomes.
  Evidence: Human review of reports/EXP-001.md against the recorded dataset and metric definitions.

## Required check IDs

unit

## Non-goals

- Do not retrain the baseline or change its outputs.
- Do not upload datasets or large checkpoints.

## Stop conditions

- Required data is unavailable.
- The experiment requires an unapproved GPU job or broader scope.

## Research metadata

```json
{
  "hypothesis": "Removing the selected component may change the evaluation metric; the direction is not assumed.",
  "datasets": [
    "local-evaluation-dataset@REPLACE_WITH_VERSION"
  ],
  "seeds": [
    42
  ],
  "metrics": [
    "accuracy"
  ],
  "artifact_paths": [
    "reports/EXP-001.md"
  ]
}
```

## Delivery

draft_pr
