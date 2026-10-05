# AI Grader — Design Doc

## 1. Product overview

**AI Grader** is a mobile-first web app that helps teachers grade homework and
exams. The teacher photographs students' papers with a phone, **speaks** the
grading rules out loud (no typing required), and the app calls the OpenAI API
to grade every question against those rules, tally totals, and produce
class-wide statistics with CSV export.

**Target user:** K-12 and college teachers who grade paper homework/exams and
want to cut repetitive grading work without giving up final judgment — every
AI score is reviewable and adjustable before it is saved.

**Design principles**

- The teacher stays in control: AI proposes, the teacher disposes. Scores are
  editable; nothing is final until saved.
- Voice-first rubric entry: speaking is faster than typing a rubric, and the
  spoken rules are normalized into a structured, editable form.
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
- **AI layer:** `grader.py` — two OpenAI calls (rubric structuring, paper
  grading), both with `response_format: {"type": "json_object"}` and a
  loose-JSON fallback parser (strips markdown fences, extracts outermost braces).
- **Storage:** SQLite (`classes`, `assignments`, `grades` tables); uploaded
  photos in `uploads/`.

## 3. User flows

### 3.1 Set up a class and an assignment
Home (`/`) → create a class → create a grading assignment under it → land on
the assignment setup page (`/assignment/<id>`).

### 3.2 Enter the rubric (voice → structure → edit → save)
1. Click **🎤 Record rubric by voice** — the Web Speech API transcribes live
   into the notes box (Chrome/Edge recommended).
2. Click **✨ Generate Structured Rubric** — `POST /api/structure_rubric`
   sends the transcript to OpenAI, which returns per-question
   `{q, max_score, criteria}` items.
3. The items render as an editable table: add/remove rows, change the question
   label, max score, or scoring rules by typing.
4. (Alternative) **Plain-text mode**: type the rubric directly; used when the
   structured table is empty.
5. Click **💾 Save Rubric** — `POST /assignment/<id>/save_rubric` stores both
   the structured JSON and an auto-generated plain-text rendering.
6. Optionally upload reference/answer-key photos (`POST
   /assignment/<id>/upload_reference`) — they are sent along as grading context.

### 3.3 Grade papers
1. Open **📷 Grade** (`/grade/<id>`), enter the student name (voice input
   supported), take/upload one or more paper photos.
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
| POST | `/api/structure_rubric` | Transcript → structured rubric items (JSON: `{transcript}` → `{ok, items}`) |
| POST | `/assignment/<id>/save_rubric` | Save rubric (JSON: `{mode, items}` or `{mode:"text", raw_text}`) |
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

**Rubric structuring** (`STRUCTURE_SYSTEM`): frames the model as a grading lead;
input is the raw transcript; output is JSON-only `{"items": [...]}`. If a max
score is missing, the model assigns a reasonable default and flags it
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

## 7. Voice input approach

- Browser-native **Web Speech API** (`SpeechRecognition` /
  `webkitSpeechRecognition`), `lang = "en-US"`, `continuous + interimResults`
  for live transcription. No API key, no audio leaves the browser for
  transcription.
- Used in two places: the rubric notes box and the student-name field.
- **Manual fallback everywhere**: the transcript lands in an editable textarea;
  unsupported browsers get a disabled button with an explanatory label instead
  of a silent failure.

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
- Voice transcription needs Chrome/Edge; Safari support is limited.
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
