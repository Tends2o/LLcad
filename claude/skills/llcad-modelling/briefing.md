# LLcad build briefing (condensed skill; full version ~/.claude/skills/llcad-modelling/SKILL.md)

The currency is the turn, not kernel time: 96 % of past 2 h runs was model output (patch JSON 37 %,
polling 16 %, reading LLcad source 18 %). Everything below removes turns.

## Tools and files
- S=~/.claude/skills/llcad-modelling ; operators: $S/reference/operators.md ; errors: $S/reference/pitfalls.md ;
  recipes (grip loft, tilted head, knurl, ring text, SVG logo, keys, LED window, lens seat, shell+seam): $S/reference/recipes.md
- DSL client: `python3 $S/scripts/llcad.py caps|head|create|lint|submit|schema|gates|job|call`
- Run contract + ledger: `python3 $S/scripts/ledger.py init --model <id> --budget 60 --product "<name>"`, `add`, `set`, `phase`, `assume`
- Judge crops: `python3 $S/scripts/crop.py triple --ref .. --render .. --parts view.json --part <id> -o out.png`
- Never read workers/*.py or compiler source; `llcad.py schema <operator>` answers field questions.

## Phases (60 min box, expected ~50)
0–3 Reference Pack (dims with tolerance and class A/B/C, registered views, CSV contours, parts+colours,
markings from original vectors) · 3–10 plan into `.llcad/plan.md` + ledger items · 10–19 MVP from ONE
generator, commit, gates, 4-view look · 19–32 details by ledger priority · 32–37 integrate + full gates
· 35–40 fresh-context judge on crop triples (advisory) · 40–48 ≤2 fix rounds, largest residual first ·
48–50 report + assumptions, export once at the end. Record phases with `ledger.py phase`.

## Rules
1. Compute coordinates, trigonometry, sections, glyph placement and NURBS in Python (`.llcad/gen/*.py`
   with `from llcad import Build, Frame`). Never do arithmetic in thinking; never think >20k tokens in
   one turn: plan into files.
2. Few large patches (30–64 ops); the DSL splits at 64 and chains structure hashes.
3. Submit by reference: pass the printed `cad_apply_patch {... "patch_ref": {...}}` line, or run the
   generator with `--http` (apply → wait → validate → commit in one Bash call). Never cat or re-type patch JSON.
4. No polling loops: wait_ms / hooks deliver terminal job results; at most one explicit cad_job_get.
5. Batch independent calls in one message. Read models with `llcad.py head` / brief detail.
6. Conventions: box = min corner; cylinder/cone = base centre +Z; hole/pocket/groove cut DOWN from z;
   profile planar and CCW from +Z for extrude; revolve about local Z with the profile in XZ (x ≥ 0);
   extrude only +Z of the local frame (place the solid); loft sections with equal counts; booleans ≤16
   inputs; mirror x → −x; outputs are solids; structure ids (pt-…, fr-…) ≠ feature ids (p01-…);
   ≤64 ops per patch; fresh idempotency key per attempt.
7. Frames, not transform chains: tilted head = structure frame, features with frame="fr-head".
8. Prints: text/svg operators (DSL falls back to font/SVG outlines), relief ≥0.05 mm, logos from the
   original SVG, device colour measured on photos. Part names `Material #RRGGBB · Part`.
9. Break edges on single bodies before booleans; no near-tangent cuts; export tessellation 0.12–0.18 mm.
10. After every milestone commit read 4 views (front, side, back, iso): nothing outside the device, no
    parts side by side, keys/windows visible, logo place/colour, name = content.
11. Floor, not finish: gates give floor_passed + ranked residuals; fix the largest or justify it in the
    assumptions list. A gate or VLM "match" is never "done".
12. Scope: visible exterior + main internals. Variants/states/accessories only on request, as
    configurations, never displaced copies. Out-of-scope items go into the report.
13. Stop only when: head committed, no open candidate, gate floor passed, assumptions list written,
    model name matches content. The Stop hook enforces this (`.llcad/giveup.md` = deliberate abort).
14. Lanes (parallel subagents with id prefixes) only when asked or ≥3 independent regions.

## Report
Model id + head revision, what matches with numbers, remaining residuals, assumptions ([C] items),
out-of-scope items, exports. Short, factual, no self-praise.
