# Browser E2E Report v2 — 2026-10-09 (after fixes)

Production: https://ai-grader-shwa.onrender.com
Assignment 6: "2023 AP Calc AB FRQ E2E v2", class AP calculus AB
Setup: /assignment/6, Grade: /grade/6, Results: /results/6

Input: 6 official FRQ pages (page-03, 04, 07, 08, 09, 10) = Q1-Q6, blank (no solutions).

## Phase 1: Generate Rubric — FAIL
- Generate Rubric button: clickable with NORMAL mouse click, NOT covered by sticky bar. (Fix 3 verified.)
- Detected: 3/6 (Q1, Q2, Q3). Q4/Q5/Q6 missing entirely.
- Q1: detected, 9 pts, solution NOT generated, NOT verified. Error: "Processing failed ('NoneType' object is not subscriptable)". 2 manual retries, same error.
- Q2: detected, 9 pts, solution generated YES, verified YES.
- Q3: detected, 9 pts, solution generated YES, verified YES.
- Verified: 2/3 detected (2/6 expected).

## Phase 2: Grade — PASS
- Same 6 images as student work. Name entered manually ("AP E2E Student").
- Scores: Q1 0/9, Q2 0/9, Q3 0/9. Total 0/27. Correct (no student work).
- Saved to /results/6. Timestamp 2026-10-09 14:10:05 UTC.

## Diagnosis (new issues)
1. Q1 NoneType: `_solve_with_retries` returns `(solved, None)` when solve succeeds but verify raises on all 3 attempts. `_build_from_solve` then crashes on `verdict["verdict"]`. Root cause bug in retry-exhaustion logic.
2. Detection 3/6: `digitize_example` uses max_tokens=4096 for all 6 images; likely insufficient for 6 full FRQs (or LLM non-determinism). Needs token increase or batching.

## Overall: Phase 1 FAIL, Phase 2 PASS.
