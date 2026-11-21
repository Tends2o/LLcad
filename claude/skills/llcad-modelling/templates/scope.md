# Scope and run contract

Fill in before the first patch (5 minutes, not more). The stop gate and the judge read it.

## Product
- Name / variant: <product, exact variant, e.g. Hand lamp T1, LED, 2nd generation>
- Model name in LLcad: <name that says what the model contains; no "complete" before it is>

## In scope (one-shot default)
- Visible exterior: silhouette, proportions, every visible part with its colour
- Controls, keys, windows, LEDs, markings/logos (B-Rep relief >= 0.05 mm, from original vectors)
- Main internals only where they are visible or named in the brief

## Out of scope unless the user asked
- Variants, states, accessories (if asked: IR configurations, never displaced copies next to the device)
- Invisible internals, electronics, fibre optics, seams < 0.1 mm

## Time box
- Budget <60> min. At 50 %: MVP committed and gated. At 75 %: details frozen, judge. At 90 %: report.

## Acceptance
- Gate floor passed on the head (deterministic gates; floor, not finish)
- Ledger P0 items verified with evidence bound to the head revision
- No open candidate; model name matches content; nothing outside the device envelope
- Assumptions list written (every [C] estimate, every source limit)

## Lanes (opt-in, only when asked or when the plan has >= 3 independent regions)
| lane | id prefix | parts | owner |
|------|-----------|-------|-------|
| A head/optics | pA- | ... | subagent |
| B grip/controls | pB- | ... | subagent |
| C markings | pC- | ... | subagent |
