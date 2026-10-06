# AI Grader

A web app for teachers to grade homework and exams with AI (MVP).

**Core flow**: photograph a student's paper/homework with your phone → the teacher
**speaks** the grading rules out loud (no typing needed) → speech is automatically
structured into an **editable rubric** → **OpenAI API (GPT-4o vision)** grades each
question against the rubric → totals are tallied → class-wide stats + CSV export.

- English UI, mobile-first (teachers photograph papers on their phones).
- Stack: Python + Flask + SQLite (stdlib), vanilla HTML/CSS/JS (no build step).

---

## Features

1. **Classes / assignments**: create a class → create grading assignments under it (homework/exam name).
2. **Rubric entry** (assignment setup page):
   - 📝 Reference example: take a photo or upload existing photos of a reference — a paper you already graded by hand (with your check marks, crosses, and point notes), an answer key, or a printed scoring guide. Examples are **saved** per assignment: revisit them any time, check/uncheck which ones to use, delete and replace them;
   - ✨ Generate rubric from example: the backend **digitizes** the example (question text + reference solution + rubric), then **validates** each reference solution by independently re-solving the question and Python-checking the math. Failed questions are retried once; anything still unresolved is flagged ⚠️ for you to fix (editing a flagged row marks it verified by you);
   - The digitized table is fully editable: add/remove rows, change labels, max scores, solutions, rules;
   - Optional reference/answer-key photos, sent along as grading context.
3. **Photo grading**: enter the student name → take photos or pick from the photo library (multiple pages supported, can mix both)
   → the backend sends the rubric + answer-key images + paper photos to GPT-4o, which must return strict JSON
   (per question: detected answer summary, score, max score, scoring rationale; total; overall feedback) →
   the teacher can adjust any score before saving.
4. **Results**: full class score table, average/highest/lowest, score-distribution histogram (pure CSS/JS), CSV export.
5. **Settings page**: enter the OpenAI API key (written to the local `.env`), model name configurable (default `gpt-4o`).

## Install & run

```bash
cd ai-grader
./run.sh
# or manually:
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
flask --app app run --host=0.0.0.0 --port=5000
```

Open http://localhost:5000 in a browser.

## Configure the OpenAI key

1. Sign in at https://platform.openai.com and open the **API keys** page;
2. Click **Create new secret key** and copy the generated key (starts with `sk-`, shown only once);
3. Paste it on the app's **Settings** page and save (or copy `.env.example` to `.env` and fill it in).

Tip: set usage limits (Limits) on your key in the OpenAI dashboard to avoid surprise spend.
The model is configured via the `OPENAI_MODEL` environment variable, default `gpt-4o`
(vision quality prioritized for grading).

> 🔒 Security: the key lives only in the server's local `.env` file; `.env` is in
> `.gitignore` and is never committed to git; requests go only to `api.openai.com`.

## Deployment

Run it on any machine with Python 3.11+:

```bash
cd ai-grader
./run.sh   # creates .venv, installs deps, serves http://localhost:5000
```

To run it for other devices on the same network (e.g. your phone), the Flask
command in `run.sh` already binds `0.0.0.0` — open `http://<server-ip>:5000` on
the phone. The OpenAI key goes in the local `.env` (via the Settings page);
data lives in `ai_grader.db` and paper photos in `uploads/` — back up the
directory (or export CSVs) regularly.

## Project layout

```
ai-grader/
  app.py            Flask routes (pages + API)
  grader.py         OpenAI calls: rubric-from-example vision, paper grading
  db.py             SQLite schema + CRUD (stdlib sqlite3)
  DESIGN.md         Design doc (English)
  templates/        base / index / assignment / grade / results / settings
  static/           style.css (mobile-first) / app.js (camera, grading UI)
  uploads/          uploaded paper / answer-key photos (git-ignored)
  requirements.txt  flask / openai / python-dotenv
  .env.example      environment variable template (placeholders only)
  run.sh            one-command startup
```

## Known limitations (MVP)

- Rubric generation reads your example photos with OpenAI vision (GPT-4o) — clear, well-lit shots of your markings give the best results;
- Oversized photos may exceed the model's per-image limits — take clear but not excessively zoomed shots;
- Single-file SQLite: switch to Postgres if many teachers use it concurrently.
