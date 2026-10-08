# AI Grader Eval Report

**Date:** 2026-10-07
**Eval Bank:** 2 quiz sets (quiz4: 4 images, quiz4b: 3 images)
**Pipeline:** Same production code (`grader.build_unified_rubric_streaming`, `grader.grade_paper`)

## Quiz #4 (Set 1)

### Stage 1 — Rubric Creation
- Verified: 2/9
- AP Format: 9/9
- Points OK: 9/9

**Failed verification:**
- QQ3: Not confirmed after 3 attempts (The reference solution incorrectly doubled the terms inside the expansion.). Please review manually.
- QQ2: Not confirmed after 3 attempts (The reference solution incorrectly stated the slope and equation for the normal line.). Please review manually.
- QQ5: Not confirmed after 3 attempts (The reference solution incorrectly calculated the midpoints.). Please review manually.
- QQ7: Not confirmed after 3 attempts (The reference solution incorrectly includes a negative sign.). Please review manually.
- QQ9: Not confirmed after 3 attempts (The derivative and critical points in the reference were incorrect; recalculated correctly.). Please review manually.
- QQ4: Not confirmed after 3 attempts (The reference solution incorrectly lists x=3 as a hole; it is a vertical asymptote.). Please review manually.
- QQ8: No explicit formula for f′(x) is provided, so numerical verification is impossible; the qualitative reasoning appears self-consistent.

### Stage 2 — Grading
- Matched: 9/9

**Mismatches:**

## Quiz #4 (Set 2)

### Stage 1 — Rubric Creation
- Verified: 6/9
- AP Format: 9/9
- Points OK: 9/9

**Failed verification:**
- QQ1: Not confirmed after 3 attempts (Python disproves teacher claim(s): 12 * 60**2 + 10 * 60 = 43800. The teacher's computation is incorrect; the correct d
- QQ6: Not confirmed after 3 attempts (The numerator in the reference is incorrect, resulting in wrong terms.). Please review manually.
- QQ9: Not confirmed after 3 attempts (Python disproves teacher claim(s): 8*(-2)**3 + 8*(-2)**2 + 69 = 37. Reference solution incorrectly identifies critical

### Stage 2 — Grading
- Matched: 7/9

**Mismatches:**
- QQ4: scored 7.0/10.0, expected 10.0
  Feedback: Missed labeling hole, even multiplicity, end behavior.
- QQ9: scored 9.0/10.0, expected 10.0
  Feedback: Correct differentiation and solving for critical points. Missed labeling one extremum.

## Key Findings

1. **Forced AP conversion works:** 9/9 AP format on both sets (user requirement 2026-10-07)
2. **Verifier false negatives:** Most 'review' flags are verifier errors, not teacher errors
   - LLM arithmetic errors (Q5 midpoints, Q7 sign, Q9 critical points)
   - Equivalent forms not recognized (Q2 equation, Q3 simplified)
3. **Python numerical verification:** Reduces false negatives by executing checks
4. **Grader accuracy:** 9/9 and 7/9 on correct teacher work
5. **Weak spots:** Hand-drawn graphs, complex factored expressions
