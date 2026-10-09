# Browser E2E Report — 2023 AP Calculus AB FRQ (official, blank)

- Date: 2026-10-09 07:00 UTC (PDT Oct 9, 2026)
- Production: https://ai-grader-shwa.onrender.com (teacher login, saved credentials)
- Assignment: 5, "2023 AP Calc AB FRQ E2E", class "AP calculus AB" (created new via UI)
- Reference images: page-03.png … page-08.png (6 official FRQ question pages, NO solutions)
- Purpose: exercise the generate-then-verify workflow (blank exam → AI generates solutions → verifies rubric)

## Phase 1 — Generate Rubric: PARTIAL

- All 6 images uploaded as Step-1 "Reference example", all CHECKED for inclusion.
- "Generate Rubric" button was obscured by the sticky "Go to Grading" bottom bar (element click refused); worked around with keyboard focus + Enter. **UI bug filed below.**
- Generation flow ran: "Reading questions…", "Reading pages…", "Validated n/4 questions…".
- **Detected: 4 of 6 FRQs** (official exam has 6; cross-page detection miss, consistent with earlier Stage-1 eval).

| Q | Topic | Max | Status | Reference solution |
|---|-------|-----|--------|-------------------|
| Q1 | gas-station flow rate | 9 | Needs review | EMPTY — "AI solving failed after 3 attempts (AP plan validation failed: Rubric step numbers are not sequential…)" |
| Q2 | swimmer velocity | 9 | Needs review | EMPTY — "AI solving failed after 3 attempts (The model returned an empty response. Please try again.)" |
| Q3 | milk-bottle differential equation | 9 | VERIFIED | GENERATED OK — full solution Steps 1–9, dM/dt = k(40−M), M(0)=5, M(t)=40−35e^(−t/4), Python-checked evaluations |
| Q4 | graph of f′ | 9 | Needs review | EMPTY — "AI solving failed after 3 attempts (Invalid \escape: line 2 column 390 (char 391))" |

- Retries: "Retry with AI" × 2 each for Q1/Q2/Q4 (~60s each) — all 6 retries failed with the same errors.
- Final verified: **1 of 4** (Q3 only).
- Finding: generate-then-verify executed for blank questions (intended behavior), succeeded for Q3 only; Q1/Q2/Q4 solution generation failed (validation / empty-response / JSON-escape errors) and retries did not recover.

## Phase 2 — Grade: PASS

- /grade/5, same 6 images uploaded as student papers.
- Name field EMPTY (no auto-fill — expected, official pages carry no name); entered "AP E2E Student".
- "Grade Now" finished in ~11s: Q1–Q4 all 0/9, Total 0/36. Feedback correctly noted the photos show only printed question pages with no student response.
- "Save Grade" persisted; /results/5 confirms row: NAME "AP E2E Student", TOTAL 0.0 / MAX 36.0, 2026-10-09 06:59:49 UTC. 1 student, average 0.0.

## Bugs found

1. Sticky bottom bar obscures the "Generate Rubric" button on /assignment/<id> (needed keyboard workaround). → FIXED 2026-10-09: `body:has(.sticky-bar)` reserves 92px bottom padding (static/style.css).
2. ~~Only 4 of 6 official FRQs detected from the 6 question pages.~~ → NOT AN APP BUG (diagnosed 2026-10-09): pages 3–8 contain only FRQs 1–4 (p3=Q1, p4=Q2, p5–6=Part A/B dividers, p7=Q3, p8=Q4). FRQs 5–6 are on pages 9–10, which were never uploaded. The app correctly detected all 4 questions present. Re-test must upload pages 3,4,7,8,9,10.
3. AI self-solution generation failed for Q1/Q2/Q4 (3 auto + 2 manual retries each):
   - Q1: `_check_ap_output` required globally sequential step numbers — impossible for multi-part FRQs (each part restarts at Step 1). → FIXED: per-part sequential validation.
   - Q2: 3 straight empty model responses. → FIXED: after 2 consecutive empty responses, fall back from reasoning model to DEFAULT_MODEL (gpt-4o, JSON mode).
   - Q4: `json.loads` failed on raw LaTeX backslashes ("Invalid \\escape"); worse, `\frac`-style escapes are VALID JSON decoding to control chars (silent math corruption). → FIXED: LaTeX-aware scanner repair + corruption detection in `_parse_json_loose` (rejects, never silently accepts).
   Also fixed: `validate_question` now returns `generated_solution` on ALL confirmed paths (was only one); `_chat_json` raises typed `EmptyModelResponseError`.

## Verdict

**FAIL** per strict criterion (Phase 1: 1/4 verified after allowed retries). Phase 2 mechanics passed end to end. Nothing deleted.
