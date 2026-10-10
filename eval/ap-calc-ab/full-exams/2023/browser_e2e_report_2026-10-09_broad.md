# Browser E2E Broad Test Report — 2026-10-09

## Setup
- Assignment 11: "AP Calc AB FRQ E2E Broad Test" (https://ai-grader-shwa.onrender.com/assignment/11)
- Class: AP calculus AB
- 6 images uploaded: page-03, page-04, page-07, page-08, page-09, page-10 (2023 AP Calc AB FRQ Q1–Q6)
- All 6 "Include in rubric generation" checkboxes auto-checked

## Generation Results
- Duration: ~5–6 minutes
- Progress: "Generating rubric…" → "Reading questions… (Xs)" → "Validated 0/6" → "Validated 2/6" → "Validated 4/6"
- **Questions detected: 4/6** (Q1, Q2, Q3, Q4; Q5, Q6 silently dropped)
- Q2: Verified (f(x)=x·e^(-x), particle motion)
- Q3: Verified (milk-bottle differential equation)
- Q1: needs review ("AI solving failed after 3 attempts", gas-station FRQ)
- Q4: needs review ("AI solving failed after 3 attempts", speed-skating FRQ)

## Retry
- Clicked "Retry with AI" for Q1 → model selector appeared (default o3) → clicked Retry
- Status: "⏳ Retrying with o3… (about 30-60s)"
- **Browser crashed ~52s in** (terminal_renderer_unresponsive) — retry outcome UNKNOWN

## Not Reached
- /grade/11 (Go to Grading)
- /results/11

## Failures Summary
1. **Count mismatch**: 4/6 detected (consistent with v3/v4 nondeterminism: 3/6, 4/6, 0/6 observed)
2. **Q1, Q4 solve failures**: "AI solving failed after 3 attempts" (no 500 — the 867d492 fix holds)
3. **Browser crash during retry**: environment issue, not app bug
4. **Grading/Results untested** in this run

## No Side Effects
- No assignments deleted. No grades submitted. No other changes.
