# Browser E2E Report v3 — 2026-10-09 (after SolveFailedError + digitize fixes)

Production: https://ai-grader-shwa.onrender.com
Assignment 7: "2023 AP Calc AB FRQ E2E v3", class AP calculus AB
Setup: /assignment/7, Grade: /grade/7, Results: /results/7

Input: 6 official FRQ pages (page-03, 04, 07, 08, 09, 10) = Q1-Q6, blank.

## Phase 1: Generate Rubric — PARTIAL (targeted bug fixed)
- Generate Rubric button: normal click, not obstructed.
- Detected: 4/6 (Q1 gas/table, Q2 swimming, Q3 milk/DE, Q4 f' graph). 2 FRQs missing.
- Q1 NoneType: FIXED — no "'NoneType' object is not subscriptable" anywhere. Q1 generated + verified first pass.
- Q1: solution YES, verified YES.
- Q2: solution NO, verified NO. Auto: "AI solving failed after 3 attempts." Manual retries x2: "Server returned 500."
- Q3: solution YES after 1 retry, verified YES. (Retry debug: first attempt failed validation "Solution is missing Step 1 of Part (a)".)
- Q4: solution NO, verified NO. Same as Q2 (auto fail + 2x 500 on retry).
- Final verified: 2/4 (Q1, Q3).

## Phase 2: Grade — PASS
- Scores: Q1-Q4 0/9 each. Total 0/36 (rubric has 4 questions).
- Saved to /results/7. Timestamp 2026-10-09 14:47:54 UTC.

## New issues
1. Detection 4/6 (was 3/6 in v2): token bump helped partially, not fully. Likely model attention/non-determinism, not just tokens.
2. Q2/Q4 manual retry → Server 500 (new failure mode, in retry_solve_question endpoint). Needs server log investigation.

## Overall: PARTIAL PASS. Targeted NoneType fix confirmed. Detection and 500s need work.
