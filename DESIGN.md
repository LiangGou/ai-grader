# AI Grader — Design Doc

## 1. Product overview

**AI Grader** is a mobile-first web app that helps teachers grade homework and
exams. The teacher photographs a reference example — a paper they already
graded by hand, an answer key, or a scoring guide — and the app's vision AI
reconstructs the grading rubric from the teacher's markings. Then the teacher
photographs students' papers with a phone, and the app calls the OpenAI API
to grade every question against that rubric, tally totals, and produce
class-wide statistics with CSV export.

**Target user:** K-12 and college teachers who grade paper homework/exams and
want to cut repetitive grading work without giving up final judgment — every
AI score is reviewable and adjustable before it is saved.

**Design principles**

- The teacher stays in control: AI proposes, the teacher disposes. Scores are
  editable; nothing is final until saved.
- Rubric by example: the teacher shows one graded example and the AI
  reconstructs the rubric from it — no need to write out rules from scratch.
  The generated rubric is always shown as an editable table before use.
- Zero build step: the whole app is plain Flask + vanilla JS so a teacher (or
  a school IT volunteer) can run it with one command.

## 2. Architecture

```
Browser (mobile-first HTML/CSS/JS)
   │  photos (multipart) / JSON
   ▼
Flask (app.py) ── SQLite (db.py, stdlib sqlite3, single file ai_grader.db)
   │
   │  chat completions + vision (base64 data URLs)
   ▼
OpenAI API (api.openai.com) — model configurable via OPENAI_MODEL (default gpt-4o)
```

- **Frontend:** vanilla JS in `static/app.js`; no framework, no bundler.
  Photo capture uses `<input type="file" accept="image/*" capture="environment" multiple>`.
- **Backend:** Flask routes for pages plus JSON/multipart API endpoints.
- **AI layer:** `grader.py` — two OpenAI vision calls (rubric-from-example,
  paper grading), both with `response_format: {"type": "json_object"}` and a
  loose-JSON fallback parser (strips markdown fences, extracts outermost braces).
- **Storage:** SQLite (`classes`, `assignments`, `grades` tables); uploaded
  photos in `uploads/`.

## 3. User flows

### 3.1 Set up a class and an assignment
Home (`/`) → create a class → create a grading assignment under it → land on
the assignment setup page (`/assignment/<id>`).

### 3.2 Enter the rubric (example → generate → edit → save)
1. Provide the reference example — **📸 Take Photo** (camera) or **🖼️ Photo
   Library** (existing photos): a paper you already graded by hand (with your
   check marks, crosses, circled mistakes, and handwritten point notes like
   "−2"), an answer key, or a printed scoring guide. Uploads are **saved**
   immediately to the assignment's example library (`uploads/`, tracked in
   the DB); revisit them any time, check/uncheck which ones feed the next
   generation, or delete and replace them.
2. Click **✨ Generate Rubric from Example** — `POST /api/rubric_from_example`
   sends the **checked** saved examples to OpenAI's vision API (GPT-4o), which studies your
   markings and reconstructs the rubric as per-question
   `{q, max_score, criteria}` items (e.g. a "−2" next to a missing unit becomes
   a deduction rule).
3. The items render as an editable table: add/remove rows, change the question
   label, max score, or scoring rules by typing.
4. Click **💾 Save Rubric** — `POST /assignment/<id>/save_rubric` stores both
   the structured JSON and an auto-generated plain-text rendering. Saving
   requires at least one table row; the rubric always comes from the example
   (there is no free-text rubric path).
5. Optionally upload reference/answer-key photos (`POST
   /assignment/<id>/upload_reference`) — they are sent along as grading context.

### 3.3 Grade papers
1. Open **📷 Grade** (`/grade/<id>`), type the student name, then **📸 Take
   Photo** (camera) or **🖼️ Photo Library** (existing photos) — multiple
   pages supported, both sources can be mixed; thumbnails preview the
   selection.
2. Click **🚀 Grade Now** — `POST /api/grade` sends rubric + answer-key images
   + paper photos to GPT-4o, which returns per-question scores with rationales,
   detected answers, total, and overall feedback.
3. Review each question; adjust any score (the total recalculates live).
4. Click **💾 Save Grade** — `POST /api/save_grade` persists the (possibly
   edited) result. The form resets for the next student.

### 3.4 Results
`/results/<id>` shows: graded count, average, highest, lowest; a score-bracket
histogram (pure CSS/JS); the full score table; and **⬇️ Export CSV**
(`GET /results/<id>/csv`, UTF-8 with BOM for Excel).

## 4. API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Home: classes + their assignments |
| POST | `/class/new` | Create a class (form: `name`) |
| POST | `/assignment/new` | Create an assignment (form: `class_id`, `name`) |
| GET | `/assignment/<id>` | Assignment setup page (rubric editor) |
| POST | `/api/rubric_from_example` | Saved example filenames (JSON `{assignment_id, include:[names]}`) → vision-generated rubric items (`{ok, items}`) |
| POST | `/assignment/<id>/upload_rubric_example` | Save rubric example photos (multipart `images`) → `{ok, examples}` |
| POST | `/assignment/<id>/delete_rubric_example` | Delete a saved example (JSON `{filename}`) → `{ok, examples}` |
| POST | `/assignment/<id>/save_rubric` | Save rubric (JSON: `{items}`; requires ≥1 row) |
| POST | `/assignment/<id>/upload_reference` | Upload answer-key photos (multipart `images`) |
| GET | `/grade/<id>` | Grading page |
| POST | `/api/grade` | Grade photos (multipart: `assignment_id`, `student_name`, `images`) → `{ok, result, photos}` |
| POST | `/api/save_grade` | Persist a grade (JSON: `{assignment_id, student_name, questions, total, max_total, overall_feedback}`) |
| GET | `/results/<id>` | Results & stats page |
| GET | `/results/<id>/csv` | Download results as CSV |
| GET/POST | `/settings` | View / save OpenAI key + model (writes project `.env`) |
| GET | `/uploads/<filename>` | Serve an uploaded photo |

All `/api/*` endpoints return `{ok: true, ...}` on success and
`{ok: false, error: "..."}` with a 4xx/5xx status on failure; the UI surfaces
`error` verbatim, so messages are written for teachers, not developers.

## 5. DB schema (SQLite)

```sql
classes(id INTEGER PK, name TEXT, created_at TEXT)
assignments(id INTEGER PK, class_id INTEGER → classes,
            name TEXT, rubric_text TEXT, rubric_json TEXT,   -- structured [{q,max_score,criteria}]
            reference_images TEXT,                          -- JSON list of upload paths
            created_at TEXT)
grades(id INTEGER PK, assignment_id INTEGER → assignments,
       student_name TEXT, questions_json TEXT,              -- graded per-question detail
       total REAL, max_total REAL, overall_feedback TEXT, created_at TEXT)
```

## 6. Grading prompt design (`grader.py`)

**Rubric from example** (`EXAMPLE_RUBRIC_SYSTEM`): frames the model as a
grading lead studying the teacher's reference example; input is the example
photos (hand-graded paper, answer key, or scoring guide); output is JSON-only
`{"items": [...]}`. Teacher markings are turned into rules — e.g. a "−2" next
to a missing unit becomes a deduction rule. If a max score is missing, the
model assigns a reasonable default and flags it
("Max score set by AI default — please confirm") so the teacher reviews it.
Unparseable input yields `{"items": []}` and the UI asks the teacher to check
or add rows manually.

**Paper grading** (`GRADER_SYSTEM`): frames the model as a strict grader with
four hard rules —
1. **Strict rubric adherence** — score only what is visible in the photos;
   missing/illegible/unanswered ⇒ 0 with the reason in `feedback`.
2. **Score caps** — no question may exceed its max; enforced again in code
   (`clamp: max(0, min(score, max_score))`), and totals are recomputed
   server-side from the clamped per-question scores.
3. **Describe-before-scoring** — the model must quote/summarize the detected
   student answer (`answer_seen`) before scoring, which reduces hallucination
   and gives the teacher an audit trail.
4. **JSON-only** — the schema is fixed
   (`questions[{q, score, max_score, answer_seen, feedback}]`, `total`,
   `max_total`, `overall_feedback`); `response_format: json_object` plus a
   loose parser keeps one malformed fence from breaking a grading run.

The rubric sent for grading **prefers the structured version**
(`build_rubric_prompt_text`): structured `{q, max_score, criteria}` rows
rendered as `"Question 1 (max 5 pts): …"` lines — more precise than free text.

## 7. Rubric-from-example approach

- The teacher provides a reference example (hand-graded paper, answer key, or
  scoring guide) via **📸 Take Photo** / **🖼️ Photo Library**; uploads are
  saved immediately (`POST /assignment/<id>/upload_rubric_example` → `uploads/`
  + `assignments.rubric_example_images`) and shown as a checkable, deletable
  library. `POST /api/rubric_from_example` takes the **checked** saved
  filenames (validated against the assignment's library to block path
  traversal) and calls `grader.rubric_from_example`, which sends them to
  OpenAI's vision API (GPT-4o) with a prompt to reconstruct the rubric from
  the teacher's markings (checks, crosses, "−2" notes → deduction rules).
- The generated rubric always renders as an editable table before saving —
  the teacher reviews and corrects every row.
- There is no voice input and no free-text rubric path: the rubric comes only
  from the example (or manual table edits).

## 8. Security notes

- The OpenAI key is stored **only** in the project-root `.env` on the server
  running the app — entered once on the Settings page, never typed into chat,
  never baked into the repo.
- `.gitignore` covers `.env`, `*.db`, `uploads/`, `.venv/`, `__pycache__/`;
  `.env.example` ships with a `sk-...` placeholder only.
- The Settings page never echoes the full key (only the last 4 characters as a
  hint); the password field is blank on load and "leave blank keeps the
  existing key".
- Outbound AI traffic goes only to `api.openai.com`.
- Cost hygiene: the README advises setting usage Limits on the key; the
  default model (`gpt-4o`) is chosen for grading quality, and `OPENAI_MODEL`
  makes it one variable to change.

## 9. Deployment

Local run only:

- `./run.sh` creates `.venv`, installs `requirements.txt` (`flask`, `openai`,
  `python-dotenv`), warns if `.env` is missing, and serves on
  `http://localhost:5000`.
- To grade from a phone on the same network, open `http://<server-ip>:5000` —
  `run.sh` already binds `0.0.0.0`.
- The OpenAI key lives in the local `.env` (entered once on the Settings
  page). Data lives in `ai_grader.db` (single SQLite file) and paper photos in
  `uploads/` — back up the project directory (or export CSVs) regularly.

## 10. Known limitations & future work

**Limitations (MVP)**
- Rubric generation reads your example photos with OpenAI vision (GPT-4o) — clear, well-lit shots of your markings give the best results.
- Very large photos may hit per-image model limits — clear, full-page shots
  work best.
- Single-file SQLite won't suit many concurrent teachers (→ Postgres).
- Question-number misalignment (paper's Q3 vs rubric's Q3) currently relies on
  the teacher's review.

**Future work (proposed, not yet approved)**
1. **Pre-grade "read the paper" check:** before scoring, have the model list
   the question numbers/labels it detects on the paper and warn the teacher
   when they don't line up with the rubric — the most painful real-world
   failure mode is grading the right answers against the wrong questions.
2. **Class roster + batch grading:** pre-register the student list, pick names
   from a dropdown (no typing), and grade a stack of papers continuously with
   auto-advance to the next student — grading 40+ papers one-by-one through
   the current form is too slow.
