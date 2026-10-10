# Browser E2E Regression Suite — AI Grader

Standing regression checklist for the production site
(`https://ai-grader-shwa.onrender.com`). The user runs this suite by saying
"run the regression tests" (or equivalent); new cases are appended here over
time — never removed without the user's explicit approval.

## How to run

For each case, spawn a live browser task (`browser.spawn_task`) with the case
steps pasted into `task`. Rules for every run:

- Never click AI generation/grading/retry buttons unless the case says so
  (those spend OpenAI budget).
- Never delete production data (assignments 1–17, the `Public Demo Class`).
- Test accounts created during a run are left in place and reported.
- Report per-step PASS/FAIL with observed evidence.

## Cases

### E2E-REG-001 — Teacher self-registration (added 2026-10-10)

1. Open `/register`. Verify the page shows username rules and a
   "Create an account" entry point exists on `/login`.
2. Register a new teacher (`e2e_teacher<N>`, password `E2eTestPass12345`,
   increment N if taken). EXPECTED: redirect to `/login`, NOT auto-logged in.
3. Log in as the new teacher. EXPECTED: home page `/` with an empty class list.
4. Create a class named `E2E Reg Test Class`. EXPECTED: appears on the home page.
5. Log out; log in as `public` / `public`. EXPECTED: `E2E Reg Test Class` is NOT
   visible (only `Public Demo Class`).
6. Log out; log back in as the new teacher. EXPECTED: the class is visible again.

### E2E-REG-002 — Public demo account isolation (added 2026-10-10)

1. Log in as `public` / `public`. EXPECTED: only `Public Demo Class` is visible;
   no private classes leak.
2. Open `Public Demo Assignment` (assignment 17). EXPECTED: loads with no
   visible errors.
3. Open `/settings` directly. EXPECTED: 403 for the public role.

### E2E-REG-003 — Rubric generation + grading (from browser-e2e skill)

Phase 1 — Generate rubric:
1. Open `/assignment/<id>` (or create a new assignment first).
2. Upload reference images as **"Reference example"** (Step 1 example images,
   NOT "Reference answer / answer key"). Check the example image(s) for
   inclusion in rubric generation.
3. Click "Generate Rubric"; wait up to 180s.
4. For each question showing "Needs your review": click "Retry with AI"
   (wait up to 90s, max 2 retries per question).
5. Verify: count verified questions.

Phase 2 — Grade:
1. Open `/grade/<id>`; upload the same images as student paper photos.
2. Verify student name auto-fill (or enter manually).
3. Click "Grade Now" (wait up to 120s); verify scores render.
4. Click "Save Grade"; open `/results/<id>` and verify the grade appears.

Default test image: `eval/quiz4/quiz4_p1.jpg` (handwritten "Eric Yang" for the
auto-fill check). NOTE: this case spends OpenAI budget — run only with the
user's explicit go-ahead.

## Run history

| Date       | Cases run | Result |
|------------|-----------|--------|
| 2026-10-10 | REG-001   | PASS (5/5 steps; teacher `e2e_teacher1`) |
| 2026-10-10 | REG-002   | PASS (manual verification during open-source hardening) |
