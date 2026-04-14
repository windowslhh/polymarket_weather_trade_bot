# Code Review Process — Trading Bot

## Review Blind Spots (Discovered 2026-04-14)

### 1. External rules not consulted
Review didn't pull the Gamma API `description` field or Polymarket settlement rules.
The assumption "daily_max > upper → NO wins" was treated as self-evident without
verifying what precision the settlement source (Weather Underground) uses.

### 2. Test cases used clean integers only
All locked-win tests used values like 76.0, 72.0, 68.0. No one constructed
boundary decimals (74.4, 74.5, 74.6) to stress-test float-vs-integer precision.

### 3. Float vs integer precision mismatch invisible to linters
`daily_max_f > slot.temp_upper_f` is syntactically valid when both are floats,
but semantically wrong when settlement rounds to whole degrees. Pure code review
cannot catch domain-level precision mismatches without external rule context.

### 4. One-sided locked-win logic went unquestioned
The code only locked slots *below* the daily max (condition A), completely missing
slots *above* a finalized daily max (condition B). The asymmetry was never flagged
because "daily_max can only go up" was accepted as sufficient reasoning.

### 5. No time-factor gate on daily_max
An early-morning reading of 50°F was treated identically to a post-peak 50°F.
The code had post-peak *confidence adjustment* but no hard gate preventing
locked-win signals before the daily max was actually finalized.

### 6. Paper mode masked settlement divergence
Paper mode never triggered real settlements, so the gap between bot predictions
and actual WU-rounded outcomes was never measured or surfaced.

---

## Mandatory Review Checklist (Enforced)

### Before reviewing any trading logic change:

- [ ] **Rule sourcing**: Archive the exact Gamma API `description` or Polymarket
      settlement rule text. Compare every comparison operator against the rule.

- [ ] **Settlement precision**: Answer "what precision does the settlement source
      use?" Document it. Verify all code comparisons match that precision.

- [ ] **Boundary test generation**: For any temperature comparison, auto-generate
      tests at: `boundary ± 0.01, ± 0.49, ± 0.50, ± 0.51, ± 1.0`

- [ ] **Cross-domain precision audit**: At every METAR float vs slot integer
      comparison, add an inline comment: `# wu_round: float→int for settlement`

- [ ] **Time-factor review**: Any strategy decision based on "current observation"
      must answer: "Is this observation sufficient to represent the *final* outcome?"
      If not, gate it behind a time/stability check.

- [ ] **Symmetric logic check**: If a condition applies to "below X", ask:
      "Does the symmetric condition (above X) also need handling?"

- [ ] **Reality reconciliation**: Each signal should log:
      `predicted_value, market_price, wu_round(daily_max)` for post-hoc audit.

### Regression prevention:

- [ ] **No banker's rounding**: Python's `round()` must NEVER be used for
      temperature settlement comparisons. Use `wu_round()` (half-up).

- [ ] **No raw float > int comparisons**: All slot boundary comparisons must
      go through `wu_round()`. Grep for `daily_max_f >` or `daily_max_f <`
      without `wu_round` — any hit is a bug.

- [ ] **Locked-win requires finality**: `evaluate_locked_win_signals` must
      receive `daily_max_final=True` or return empty. No exceptions.
